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

import logging
import os

from molecule_runtime.adapters.base import BaseAdapter, AdapterConfig, RuntimeCapabilities

logger = logging.getLogger(__name__)


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

        # Materialize the identity to ~/.hermes/SOUL.md (hermes-agent's Layer-1
        # Agent Identity). REQUIRED, not optional: the a2a-platform transport
        # forwards to hermes-agent WITHOUT config.system_prompt, so SOUL.md is the
        # only channel that makes a concierge speak as the Org Concierge on that
        # transport. Done here (after config.system_prompt is assembled, before the
        # workspace serves) so the file exists before the first a2a session caches
        # its prompt. The base flow never invoked this socket for hermes (the
        # "hermes skips _common_setup" gap) — call it explicitly.
        self.materialize_persona(config)

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
                "Check the workspace logs for the Hermes gateway — "
                "the molecule-a2a platform stanza in ~/.hermes/config.yaml "
                "should make hermes load the plugin and bind this port."
            )
        else:
            base = os.environ.get(
                "HERMES_API_BASE", "http://127.0.0.1:8642/v1"
            ).rstrip("/")
            health_url = base.replace("/v1", "") + "/health"
            err_hint = "Check the workspace logs for the Hermes gateway."

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

    # ------------------------------------------------------------------
    # ADR-004 adapter-socket: the MCP-config seam (native path / render /
    # present / read), OWNED by this adapter.
    # ------------------------------------------------------------------
    # Per ADR-004 §3 the per-runtime shape moves OUT of the shared engine's
    # ``mcp_render._RUNTIME_SPECS`` dispatch table and INTO the adapter. These
    # methods are FAITHFUL copies of the hermes entry that lives in the engine
    # today (``_hermes_path`` / ``render_hermes_config`` / ``_hermes_config_has``
    # / ``_read_hermes_mcp_servers``) — byte-identical native-config output is
    # mandatory (the golden-parity invariant), so the logic is copied verbatim,
    # not "improved". During this additive phase BOTH the engine table and this
    # adapter render identically; the engine phase later deletes the duplication.

    # Hermes reads its native MCP servers from ~/.hermes/config.yaml under a
    # top-level ``mcp_servers:`` map (NOT claude's ``mcpServers``). Kept in
    # LOCKSTEP with _read_hermes_mcp_servers (same HERMES_HOME-or-HOME
    # resolution) so a server the renderer writes is byte-for-byte the file the
    # adapter enumerates.
    _HERMES_MCP_KEY = "mcp_servers"

    @staticmethod
    def _hermes_config_path() -> str:
        """Absolute path to hermes' native ``~/.hermes/config.yaml``.

        HERMES_HOME overrides the dir; the container sets HERMES_HOME=/tmp/.hermes
        with HOME=/tmp so both agree. Mirrors the engine's ``_hermes_path`` and the
        home resolution ``_read_hermes_mcp_servers`` uses.
        """
        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.path.expanduser("~"), ".hermes"
        )
        return os.path.join(home, "config.yaml")

    def mcp_settings_path(self, config: AdapterConfig) -> str:
        """ADR-004 socket — native MCP-config file THIS runtime reads from.

        hermes ignores ``config.config_path`` and resolves its own home
        (HERMES_HOME-or-HOME), so the arg is unused but the signature is uniform.
        """
        return self._hermes_config_path()

    def register_mcp_server_hook(
        self, config: AdapterConfig, name: str, spec: dict
    ) -> None:
        """ADR-004 socket — additively merge ``name -> spec`` into hermes' native
        ``~/.hermes/config.yaml`` ``mcp_servers`` map. Idempotent; preserves the
        rest of the file (the ``model`` block, the a2a ``molecule`` url sidecar,
        any other server or hand-written key).

        FAITHFUL copy of the engine's ``render_hermes_config`` — writes the stdio
        descriptor (``{command, args?, env?}``) verbatim under
        ``mcp_servers.<name>`` and re-serializes with ``yaml.safe_dump(...,
        sort_keys=False)`` (byte-identical to the engine renderer). The base hook
        enriches the privileged management-MCP spec via ``inject_privileged_env``
        BEFORE writing (no-op for non-management names; idempotent;
        descriptor-wins), preserved here so a direct caller (the
        ensure_management_mcp self-heal) gets the same enrichment as the install
        funnel.
        """
        import yaml

        from molecule_runtime.privileged_mcp_env import inject_privileged_env

        # Belt-and-suspenders: enrich the privileged MCP spec for a caller that
        # invokes this hook DIRECTLY. No-op for non-management names; idempotent +
        # descriptor-wins. Matches BaseAdapter.register_mcp_server_hook.
        spec = inject_privileged_env(name, spec)

        from pathlib import Path

        settings_path = Path(self._hermes_config_path())
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        if settings_path.is_file():
            try:
                data = yaml.safe_load(settings_path.read_text())
            except (OSError, yaml.YAMLError):
                data = {}
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}

        servers = data.get(self._HERMES_MCP_KEY)
        if not isinstance(servers, dict):
            servers = {}
        servers[name] = spec
        data[self._HERMES_MCP_KEY] = servers

        settings_path.write_text(yaml.safe_dump(data, sort_keys=False))

    def management_mcp_present(self, config: AdapterConfig) -> bool:
        """ADR-004 socket — True when hermes' native ``~/.hermes/config.yaml``
        declares the management ``molecule-platform`` MCP under ``mcp_servers``.

        FAITHFUL copy of the engine's ``_hermes_config_has``. Fail-CLOSED by
        construction: a missing / unreadable / malformed / structurally-unexpected
        config yields ``False``, so a genuinely MCP-less hermes concierge stays
        degraded at the RCA#2970 gate.
        """
        import yaml

        from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

        try:
            data = yaml.safe_load(open(self._hermes_config_path(), encoding="utf-8").read())
        except (OSError, yaml.YAMLError):
            return False
        if not isinstance(data, dict):
            return False
        servers = data.get(self._HERMES_MCP_KEY)
        return isinstance(servers, dict) and MANAGEMENT_MCP_NAME in servers

    def materialize_persona(self, config: AdapterConfig) -> "object | None":
        """ADR-004 socket — write hermes' identity to ``~/.hermes/SOUL.md``.

        hermes-agent reads its **Layer-1 Agent Identity** from
        ``$HERMES_HOME/SOUL.md`` (pinned against NousResearch/hermes-agent's own
        prompt-assembly doc, verified live). This is the ONLY channel that reaches
        the model on the **a2a-platform transport** (``MOLECULE_A2A_PLATFORM_ENABLED
        =true``), which forwards to hermes-agent's ``/a2a/inbound`` WITHOUT
        ``config.system_prompt`` — so a concierge relying on the ``{role:system}``
        message alone boots as the base model ("I'm MiniMax-M3") instead of the Org
        Concierge. Materializing SOUL.md fixes that: hermes-agent adopts the
        identity for every session.

        Source is ``config.system_prompt`` — the base-assembled prompt, which for a
        concierge carries the persona via ``plugin_rules`` (the management plugin's
        ``rules/concierge-identity.md``); ``prompt_files`` is empty for a concierge,
        so ``read_canonical_persona`` alone would miss it. Runs in ``setup()`` before
        the workspace serves, so SOUL.md exists before the first a2a session builds
        its (cached) system prompt. Never bricks boot — a persona is not a
        privileged capability.

        ADR-004: the SOUL.md writer is OWNED by this adapter (was
        ``persona_render.materialize_hermes_persona`` dispatch). It resolves the
        HOME-based ``$HERMES_HOME/SOUL.md`` (HERMES_HOME-or-HOME, the SAME
        resolution ``_read_hermes_mcp_servers`` uses) and writes byte-identical to
        the engine's ``_write_persona_file`` (parents created, trailing newline
        appended only when absent). ``read_canonical_persona`` is the one generic,
        runtime-name-free helper the engine keeps."""
        from molecule_runtime.persona_render import read_canonical_persona

        persona = (config.system_prompt or "").strip() or (
            read_canonical_persona(config.config_path, config.prompt_files) or ""
        ).strip()
        if not persona:
            return None
        try:
            path = self._materialize_hermes_soul(persona)
            logger.info("materialize_persona: wrote hermes identity to %s", path)
            return path
        except Exception:  # noqa: BLE001 — identity is not privileged; never brick boot
            logger.warning(
                "materialize_persona: failed to write ~/.hermes/SOUL.md", exc_info=True
            )
            return None

    @staticmethod
    def _materialize_hermes_soul(persona: str) -> "object":
        """Write ``persona`` to hermes-agent's Layer-1 identity file
        ``$HERMES_HOME/SOUL.md`` (~/.hermes/SOUL.md); returns the path written.

        ADR-004 adapter-owned (faithful copy of the engine's
        ``persona_render.materialize_hermes_persona`` + ``_hermes_persona_path`` +
        ``_write_persona_file`` — byte-identical output is the golden-parity
        invariant). ``config_path`` is unused: the identity file is HOME-based,
        resolved the same HERMES_HOME-or-HOME way as ``_read_hermes_mcp_servers``
        so persona + MCP config resolve against the same hermes home."""
        from pathlib import Path

        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.path.expanduser("~"), ".hermes"
        )
        target = Path(home) / "SOUL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        body = persona if persona.endswith("\n") else persona + "\n"
        target.write_text(body, encoding="utf-8")
        return target

    def mcp_launch_env(self, config: AdapterConfig) -> dict:
        """ADR-004 socket — DYNAMIC launch-env overlay for spawned MCP servers.

        THE BUG this fixes: the hermes image bundles Node 22 under
        ``$HERMES_HOME/node/bin`` (node/npm/npx, installed by the hermes installer)
        but that dir is OFF the ``molecule-runtime`` process PATH — PATH is just the
        system dirs. So when the runtime spawns the management MCP as a stdio child
        (``npx @molecule-ai/mcp-server``) the child cannot resolve ``npx``/``node``,
        the server never launches, ``loaded_mcp_tools`` never enumerates, the online
        gate never flips, and the concierge is stuck "provisioning" forever.

        The prior fix baked ``export PATH=…/.hermes/node/bin:$PATH`` into the
        Dockerfile — a STATIC image-build hardcode. ADR-004 says the ADAPTER owns
        this runtime-specific concern and resolves it DYNAMICALLY at launch: here we
        resolve the bundled node bin dir the SAME HERMES_HOME-or-HOME way the rest of
        this adapter resolves hermes' home (so node + MCP config + persona all agree
        on one home), VERIFY node/npx actually exist there, and only then prepend it
        to a PATH overlay merged into each spawned MCP server's env. If the bin dir
        or the binaries are absent (e.g. a system-node image), we return ``{}`` — a
        clean no-op that lets the child inherit the process PATH unchanged. Nothing
        static, nothing baked; the engine names no runtime and reads no path.
        """
        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.path.expanduser("~"), ".hermes"
        )
        node_bin = os.path.join(home, "node", "bin")
        # Verify the interpreter is actually present before injecting — never claim a
        # PATH that doesn't resolve (that would just move the failure, not fix it).
        if not all(
            os.path.exists(os.path.join(node_bin, exe)) for exe in ("node", "npx")
        ):
            logger.info(
                "mcp_launch_env: no bundled node/npx under %s — inheriting process "
                "PATH unchanged (system node assumed)", node_bin,
            )
            return {}
        existing = os.environ.get("PATH", "")
        new_path = f"{node_bin}:{existing}" if existing else node_bin
        logger.info(
            "mcp_launch_env: prepending hermes bundled node bin dir %s to the "
            "spawned MCP server PATH (was off the process PATH)", node_bin,
        )
        return {"PATH": new_path}

    async def enumerate_loaded_mcp_tools(
        self, config: AdapterConfig
    ) -> "list[str] | None":
        """runtime#181 — hermes owns MCP-tool discovery for its OWN config shape.

        The RUNTIME CONTRACT default (adapter_base) reads a ``.claude``-style
        ``settings.json``; hermes has none. hermes declares its MCP servers in its
        native ``~/.hermes/config.yaml`` ``mcp_servers:`` block (written by
        start.sh). Override the contract to read THAT file and hand the resolved
        ``{name: spec}`` map to the shared boot-safe probe engine, so a hermes
        concierge's stdio ``molecule-platform`` management MCP is enumerated and
        the ``mcp__molecule-platform__provision_workspace`` gate id reaches the
        first heartbeat WITHOUT a user turn — the same online-without-a-turn
        behaviour the base default gives claude/codex/openclaw, but keyed off
        hermes' own config file (core's per-runtime enumeration switch previously
        mapped hermes to ``{}`` — the runtime#181 false-degraded root cause).

        Tri-state + never-raise are inherited from ``enumerate_from_specs_async``:
        ``None`` when nothing is declared/observed, ``[]`` for a zero-tool server,
        ``[ids]`` otherwise.
        """
        from molecule_runtime.loaded_mcp_tools_probe import enumerate_from_specs_async

        specs = self._read_hermes_mcp_servers()
        if not specs:
            return None
        # Pass the dynamic launch-env overlay so the spawned management MCP child
        # can resolve hermes' bundled node/npx (off the process PATH) — the fix for
        # the stuck-"provisioning" concierge, resolved at launch, not baked.
        return await enumerate_from_specs_async(
            specs, launch_env=self.mcp_launch_env(config)
        )

    @staticmethod
    def _read_hermes_mcp_servers() -> dict:
        """Parse hermes' native ``~/.hermes/config.yaml`` ``mcp_servers:`` block
        into the runtime-agnostic ``{name: {command, args?, env?}}`` descriptor
        map, keeping only STDIO (command-based) servers.

        The url-transport a2a sidecar (``molecule`` -> ``http://127.0.0.1:9100/mcp``)
        can't be stdio-spawned, so it's skipped — the online gate keys on the stdio
        ``molecule-platform`` management server's ``provision_workspace`` regardless.
        Returns ``{}`` on any missing-file / parse error, or when no stdio server is
        declared (an ordinary, non-concierge hermes workspace). Never raises — a
        discovery failure must fall through to the grace window, never crash boot.
        """
        import yaml

        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.path.expanduser("~"), ".hermes"
        )
        path = os.path.join(home, "config.yaml")
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            return {}
        servers = data.get("mcp_servers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return {}
        return {
            name: spec
            for name, spec in servers.items()
            if isinstance(spec, dict)
            and isinstance(spec.get("command"), str)
            and spec["command"].strip()
        }


Adapter = HermesAgentAdapter
