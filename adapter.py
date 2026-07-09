"""Hermes adapter — bridges molecule A2A to the real Nous Research hermes-agent.

This template runs the actual `hermes-agent` (github.com/NousResearch/hermes-agent)
inside the workspace container. start.sh boots `hermes gateway` with the
API_SERVER platform enabled (listening on 127.0.0.1:8642) before exec'ing
`molecule-runtime`. At request time the executor proxies every A2A
message into hermes-agent's OpenAI-compatible /v1/chat/completions
endpoint, collects the response, and emits it back on the A2A queue.

The adapter deliberately does no model/provider selection of its own —
that responsibility lives inside hermes-agent (`hermes model`, `hermes
config set`). Trying to layer a second provider registry on top was the
core mistake the previous version of this template made; see
docs/PLANNING.md for the rewrite rationale.
"""
from __future__ import annotations

import os

from molecule_runtime.adapters.base import BaseAdapter, AdapterConfig, RuntimeCapabilities


class HermesAgentAdapter(BaseAdapter):
    """Adapter that proxies A2A requests to a locally-running hermes-agent."""

    @staticmethod
    def name() -> str:
        return "hermes"

    @staticmethod
    def display_name() -> str:
        return "Hermes Agent (Nous Research)"

    @staticmethod
    def description() -> str:
        return (
            "Runs the real Nous Research hermes-agent with its native "
            "terminal, file, web, memory, and skill tools. Model + provider "
            "are owned by hermes-agent itself (hermes model)."
        )

    @staticmethod
    def get_config_schema() -> dict:
        return {
            "model": {
                "type": "string",
                "description": (
                    "Model string passed through to hermes-agent. Accepts "
                    "any form hermes-agent understands — e.g. "
                    "'nousresearch/hermes-4-70b', 'anthropic/claude-sonnet-4-5', "
                    "'gemini/gemini-2.5-pro', 'MiniMax-M2.7', or "
                    "'openrouter/<slug>'."
                ),
            },
        }

    def capabilities(self) -> RuntimeCapabilities:
        """Hermes-agent owns several cross-cutting capabilities natively
        — see project memory `project_runtime_native_pluggable.md`.

        provides_native_session=True
            hermes-agent runs an in-container event log (memory or Redis,
            configurable via runtime.event_log.backend in its own
            config.yaml) that holds in-flight session state across A2A
            turns. The platform's a2a_queue would double-buffer the
            same state — declaring native_session lets the platform
            skip enqueueing and dispatch directly. Validates capability
            primitive #5 once that consumer lands.

        Other capabilities stay False (platform fallback owns them):
        - provides_native_heartbeat: hermes-agent doesn't broadcast
          progress events at the platform's cadence; we keep emitting
          WORKSPACE_HEARTBEAT every 30s from heartbeat.py so the canvas
          UI's idle indicator stays accurate.
        - provides_native_scheduler: hermes-agent has no built-in cron;
          platform scheduler keeps owning it.
        - provides_native_status_mgmt: hermes-agent doesn't surface a
          ready/degraded/failed signal back to us; platform's
          error_rate inference still drives the workspace status.
        - provides_native_retry / activity_decoration / channel_dispatch:
          not implemented in hermes-agent's API server — platform
          fallback applies.
        """
        return RuntimeCapabilities(
            provides_native_session=True,
        )

    def idle_timeout_override(self) -> int:
        """hermes-agent synthesis on slower providers (anthropic Opus,
        custom models behind hermes' provider router) routinely exceeds
        the platform default 5min idle window. The single-text reply
        path also doesn't broadcast tool-call progress events while the
        upstream LLM is thinking — so the platform's broadcaster-silence
        timer would cancel a legit-but-slow synthesis. 15 min covers
        every observed turn so far without leaving genuinely-wedged
        runs hanging too long.

        Capability primitive #2 — see workspace/adapter_base.py:
        idle_timeout_override and PR #2139 for the platform-side
        consumer in a2a_proxy.dispatchA2A.
        """
        return 900  # 15 minutes

    async def setup(self, config: AdapterConfig) -> None:
        """Verify the hermes-agent API surface this workspace will use.

        start.sh boots `hermes gateway` before molecule-runtime. With
        MOLECULE_A2A_PLATFORM_ENABLED=true (default) we probe the
        plugin's /a2a/health endpoint; otherwise we fall back to the
        legacy api-server /health. Failing here marks the workspace
        unhealthy rather than silently forwarding to a dead port.
        """
        # --- SSOT: publish the single base-built system prompt onto config ---
        # The hermes executor consumes ``config.system_prompt`` as the
        # ``{"role": "system"}`` message it sends to hermes-agent's
        # /v1/chat/completions (executor.py ``_build_initial_messages``). That
        # field is BASE-OWNED and is None until something fills it. Build it
        # HERE via the one canonical builder (``build_system_prompt``), which
        # honors ``config.prompt_files`` (with the legacy ``system-prompt.md``
        # fallback baked in) — so a hermes concierge gets the SAME prompt-file
        # resolution every other runtime gets, instead of an empty system
        # message. This closes the per-runtime prompt drift (the executor must
        # never re-read /configs/system-prompt.md itself and ignore
        # prompt_files). Published BEFORE the smoke short-circuit so the field
        # is always set on the config the executor receives.
        #
        # Plugins: load the declared set FIRST (pure filesystem read) so the
        # plugin-shipped rules/prompt fragments fold into the assembled prompt
        # — the same shape codex/claude-code build. hermes never consumes
        # /configs/CLAUDE.md, so the prompt is the ONLY channel its model
        # receives always-on plugin rules through.
        from molecule_runtime.plugins import load_plugins
        from molecule_runtime.prompt import build_system_prompt

        workspace_plugins_dir = os.path.join(config.config_path, "plugins")
        plugins = load_plugins(
            workspace_plugins_dir=workspace_plugins_dir,
            shared_plugins_dir=os.environ.get("PLUGINS_DIR", "/plugins"),
        )
        config.system_prompt = build_system_prompt(
            config.config_path,
            config.workspace_id,
            [],  # skills: hermes owns its native skill tools, not the prompt
            [],  # peers: appended live per-turn via _fetch_peers_blurb
            prompt_files=config.prompt_files,
            plugin_rules=getattr(plugins, "rules", None),
            plugin_prompts=list(getattr(plugins, "prompt_fragments", []) or []),
        )

        # Boot-smoke contract (molecule-core#2275): start.sh's smoke-mode
        # branch exec's molecule-runtime without spawning the gateway,
        # so neither the plugin port nor :8642 is listening. Skip the
        # health probe under smoke mode — the runtime's smoke
        # short-circuit fires after create_executor() returns.
        if os.environ.get("MOLECULE_SMOKE_MODE") == "1":
            return

        # --- Plugin pipeline: drive the base per-runtime adaptor registry ---
        # Until now this template called the plugin pipeline ZERO times — the
        # declared plugins fetched into /configs/plugins by the runtime's
        # boot-install (plugin_sources.py) were never INSTALLED on hermes: no
        # skills copied to /configs/skills, no rules injected, no MCP wired.
        # This is the same call codex/openclaw/claude-code make from their own
        # setup(). For a skills-shaped plugin it resolves AgentskillsAdaptor,
        # which copies each skill into /configs/skills — the canonical dir the
        # runtime's skills-surfacing PORT (skills_render) points hermes-agent
        # at via ``skills.external_dirs`` in $HERMES_HOME/config.yaml, so the
        # plugin's skills become natively visible to hermes' skills_list.
        # For an MCP-server plugin the base hook dispatches to mcp_render,
        # whose hermes renderer writes ~/.hermes/config.yaml
        # mcp_servers.<name> -- the native MCP map hermes-agent reads. If a
        # privileged management MCP cannot be rendered, the base pipeline still
        # raises PrivilegedPluginInstallError so the boot fails CLOSED + loudly
        # instead of the previous SILENT no-install failure mode: a concierge
        # that looks online but can never create_workspace.
        # Runs AFTER the smoke short-circuit: smoke boots have no plugins
        # volume and must not execute plugin setup.sh scripts.
        await self.install_plugins_via_registry(config, plugins)

        try:
            import httpx  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Hermes adapter bridge requires httpx — "
                "add to requirements.txt and rebuild the image."
            ) from exc

        import httpx

        # Default off — see executor.py module docstring. Workspace boot
        # was wedging on the plugin /a2a/health probe because the plugin
        # didn't bind :8645 inside the deployed image. Falls back to the
        # legacy /v1/chat/completions /health probe until that's fixed.
        use_plugin = os.environ.get(
            "MOLECULE_A2A_PLATFORM_ENABLED", "false"
        ).strip().lower() in ("1", "true", "yes", "on")

        if use_plugin:
            host = os.environ.get("MOLECULE_A2A_PLATFORM_HOST", "127.0.0.1")
            port = int(os.environ.get("MOLECULE_A2A_PLATFORM_PORT", "8645"))
            health_url = f"http://{host}:{port}/a2a/health"
            err_hint = (
                "Check /var/log/hermes-gateway.log inside the container — "
                "the molecule-a2a platform stanza in ~/.hermes/config.yaml "
                "should make hermes load the plugin and bind this port."
            )
        else:
            base = os.environ.get(
                "HERMES_API_BASE", "http://127.0.0.1:8642/v1"
            ).rstrip("/")
            health_url = base.replace("/v1", "") + "/health"
            err_hint = "Check /var/log/hermes-gateway.log inside the container."

        # AsyncClient — sync httpx inside an async setup() can deadlock
        # against an aiohttp server sharing the same event loop (only
        # bites in tests; real deployments separate processes).
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(health_url)
                r.raise_for_status()
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                f"hermes-agent surface not reachable at {health_url}. {err_hint}"
            ) from exc

    async def create_executor(self, config: AdapterConfig):
        from executor import HermesAgentProxyExecutor

        executor = HermesAgentProxyExecutor(config)
        await executor.start()
        return executor


Adapter = HermesAgentAdapter
