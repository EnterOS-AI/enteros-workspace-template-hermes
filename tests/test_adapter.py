"""Tests for adapter.py.

Coverage targets the public adapter surface:
  - Static introspection (name, display_name, description, schema)
  - Capabilities (provides_native_session=True, others False)
  - idle_timeout_override (15min)
  - setup() smoke-mode short-circuit
  - setup() health probe via plugin path (default) and chat_completions
    path (when MOLECULE_A2A_PLATFORM_ENABLED=false)
  - create_executor() returns a started executor
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

import pytest
import yaml
from aiohttp import web

from adapter import HermesAgentAdapter, Adapter
from molecule_runtime.adapters.base import AdapterConfig


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _workspace_env(monkeypatch):
    monkeypatch.setenv("WORKSPACE_ID", "ws-hermes-test")
    monkeypatch.setenv("PLATFORM_URL", "http://127.0.0.1:8080")


# ---- structural -----------------------------------------------------


def test_adapter_alias():
    assert Adapter is HermesAgentAdapter


def test_static_introspection():
    assert HermesAgentAdapter.name() == "hermes"
    assert HermesAgentAdapter.display_name() == "Hermes Agent (Nous Research)"
    desc = HermesAgentAdapter.description()
    assert "Nous Research" in desc
    schema = HermesAgentAdapter.get_config_schema()
    assert "model" in schema
    assert schema["model"]["type"] == "string"


def test_capabilities_provide_native_session():
    caps = HermesAgentAdapter().capabilities()
    assert caps.provides_native_session is True


def test_idle_timeout_override_is_15_min():
    assert HermesAgentAdapter().idle_timeout_override() == 900


# ---- setup() lifecycle ----------------------------------------------


@pytest.mark.asyncio
async def test_setup_skips_under_smoke_mode(monkeypatch):
    monkeypatch.setenv("MOLECULE_SMOKE_MODE", "1")
    # No HTTP server running anywhere — would fail if probe was attempted.
    cfg = AdapterConfig(model="hermes-test")
    await HermesAgentAdapter().setup(cfg)


@pytest.mark.asyncio
async def test_setup_probes_plugin_health_when_enabled(monkeypatch):
    """When MOLECULE_A2A_PLATFORM_ENABLED=true, setup() probes
    /a2a/health (NOT the legacy /v1/health). Plugin path is opt-in
    while the image-side install is being verified — see executor.py
    module docstring."""

    monkeypatch.delenv("MOLECULE_SMOKE_MODE", raising=False)
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "true")

    health_port = _free_port()
    paths_hit: list[str] = []

    async def health_handler(request: web.Request) -> web.Response:
        paths_hit.append(request.path)
        return web.json_response({"ok": True, "platform": "molecule-a2a"})

    app = web.Application()
    app.router.add_get("/a2a/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", health_port)
    await site.start()

    try:
        monkeypatch.setenv("MOLECULE_A2A_PLATFORM_PORT", str(health_port))
        cfg = AdapterConfig(model="hermes-test")
        await HermesAgentAdapter().setup(cfg)
    finally:
        await site.stop()
        await runner.cleanup()

    assert paths_hit == ["/a2a/health"]


@pytest.mark.asyncio
async def test_setup_probes_chat_completions_health_when_disabled(monkeypatch):
    monkeypatch.delenv("MOLECULE_SMOKE_MODE", raising=False)
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "false")

    api_port = _free_port()
    paths_hit: list[str] = []

    async def health_handler(request: web.Request) -> web.Response:
        paths_hit.append(request.path)
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", api_port)
    await site.start()

    try:
        monkeypatch.setenv(
            "HERMES_API_BASE", f"http://127.0.0.1:{api_port}/v1"
        )
        cfg = AdapterConfig(model="hermes-test")
        await HermesAgentAdapter().setup(cfg)
    finally:
        await site.stop()
        await runner.cleanup()

    assert paths_hit == ["/health"]


# ---- create_executor lifecycle ---------------------------------------


@pytest.mark.asyncio
async def test_create_executor_returns_started_executor(monkeypatch):
    """create_executor() with plugin path enabled must return an
    executor whose reply server is already running (start() was
    called). Plugin path is opt-in while the image-side install is
    verified — see executor.py module docstring."""

    cb_port = _free_port()
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MOLECULE_A2A_CALLBACK_PORT", str(cb_port))

    cfg = AdapterConfig(model="hermes-test")
    executor = await HermesAgentAdapter().create_executor(cfg)
    try:
        assert executor._started is True
        assert executor._reply_runner is not None
        assert executor._reply_site is not None
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_create_executor_when_plugin_disabled_skips_reply_server(monkeypatch):
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "false")
    cfg = AdapterConfig(model="hermes-test")
    executor = await HermesAgentAdapter().create_executor(cfg)
    try:
        assert executor._started is True
        # No reply server when fallback path is in use.
        assert executor._reply_runner is None
        assert executor._reply_site is None
    finally:
        await executor.stop()


# ---- system-prompt SSOT (task #76) -----------------------------------
# The hermes executor consumes ``config.system_prompt`` as the
# ``{"role": "system"}`` message it sends to /v1/chat/completions
# (executor.py ``_build_initial_messages``). These tests pin the OTHER half:
# ``adapter.setup()`` must PUBLISH that field via the single base builder
# (``build_system_prompt``), which honors ``config.prompt_files``. Before this
# fix hermes never called the builder, so ``config.system_prompt`` stayed None
# and the system message was empty — the identity-less concierge bug. The
# invariant: ONE source (build_system_prompt honoring prompt_files), never a
# per-runtime re-read of /configs/system-prompt.md that ignores prompt_files.


@pytest.mark.asyncio
async def test_setup_publishes_system_prompt_honoring_prompt_files(
    monkeypatch, tmp_path
):
    """setup() fills config.system_prompt from the declared prompt_files, not a
    blind system-prompt.md re-read. Runs under smoke mode so it needs no live
    hermes gateway (the publish happens before the smoke short-circuit)."""
    monkeypatch.setenv("MOLECULE_SMOKE_MODE", "1")

    configs = tmp_path / "configs"
    (configs / "prompts").mkdir(parents=True)
    # Concierge layout: identity declared via prompt_files...
    (configs / "prompts" / "concierge.md").write_text("ORG-CONCIERGE-IDENTITY")
    # ...with a STALE root system-prompt.md that must NOT shadow it.
    (configs / "system-prompt.md").write_text("STALE-GENERIC-FALLBACK")

    cfg = AdapterConfig(
        model="hermes-test",
        config_path=str(configs),
        workspace_id="ws-hermes-concierge",
        prompt_files=["prompts/concierge.md"],
    )
    await HermesAgentAdapter().setup(cfg)

    assert cfg.system_prompt, "setup() left config.system_prompt empty"
    # The declared prompt file is loaded...
    assert "ORG-CONCIERGE-IDENTITY" in cfg.system_prompt
    # ...and the stale single-file fallback is NOT (prompt_files wins).
    assert "STALE-GENERIC-FALLBACK" not in cfg.system_prompt
    # The base platform identity frame is always present (single builder).
    assert "Molecule AI platform" in cfg.system_prompt


@pytest.mark.asyncio
async def test_executor_initial_messages_use_published_prompt(
    monkeypatch, tmp_path
):
    """End-to-end: the system message the executor builds is exactly the prompt
    setup() published — no second source, prompt_files honored."""
    monkeypatch.setenv("MOLECULE_SMOKE_MODE", "1")

    configs = tmp_path / "configs"
    (configs / "prompts").mkdir(parents=True)
    (configs / "prompts" / "concierge.md").write_text("ORG-CONCIERGE-IDENTITY")
    (configs / "system-prompt.md").write_text("STALE-GENERIC-FALLBACK")

    from executor import HermesAgentProxyExecutor

    cfg = AdapterConfig(
        model="hermes-test",
        config_path=str(configs),
        workspace_id="ws-hermes-concierge",
        prompt_files=["prompts/concierge.md"],
    )
    await HermesAgentAdapter().setup(cfg)

    # Build the executor over the SAME config and inspect the system message
    # it emits — without starting the reply server (we never call start()).
    executor = HermesAgentProxyExecutor(cfg)
    messages = executor._build_initial_messages("hello")

    system = [m for m in messages if m["role"] == "system"]
    assert system, "no system message emitted"
    assert "ORG-CONCIERGE-IDENTITY" in system[0]["content"]
    assert "STALE-GENERIC-FALLBACK" not in system[0]["content"]
    # The system content is sourced from the published config field.
    assert cfg.system_prompt in system[0]["content"]


# ---- setup() plugin pipeline (skills-surfacing contract) -------------


@pytest.mark.asyncio
async def test_setup_drives_plugin_pipeline(monkeypatch, tmp_path):
    """setup() must INSTALL the declared plugins via the base per-runtime
    adaptor registry — the call this template previously never made, leaving
    /configs/plugins fetched-but-never-installed on hermes (no skills in
    /configs/skills, no rules in the prompt). A skills-shaped plugin's skill
    dir must land in <configs>/skills (the canonical dir hermes-agent is
    pointed at via skills.external_dirs), and its rules must fold into the
    assembled system prompt (hermes never reads CLAUDE.md)."""

    monkeypatch.delenv("MOLECULE_SMOKE_MODE", raising=False)
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "true")
    # Kernel-off memory target: append_to_memory writes under configs.
    monkeypatch.delenv("MOLECULE_MAILBOX_KERNEL", raising=False)

    configs = tmp_path / "configs"
    plugin = configs / "plugins" / "probe-plugin"
    skill = plugin / "skills" / "probe-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: probe\n---\n\nDo the probe.\n"
    )
    rules = plugin / "rules"
    rules.mkdir()
    (rules / "always.md").write_text("ALWAYS-ON-PROBE-RULE")
    # Point the shared-plugins fallback away from the real /plugins.
    monkeypatch.setenv("PLUGINS_DIR", str(tmp_path / "no-shared-plugins"))

    health_port = _free_port()

    async def health_handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "platform": "molecule-a2a"})

    app = web.Application()
    app.router.add_get("/a2a/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", health_port)
    await site.start()

    try:
        monkeypatch.setenv("MOLECULE_A2A_PLATFORM_PORT", str(health_port))
        cfg = AdapterConfig(model="hermes-test", config_path=str(configs))
        await HermesAgentAdapter().setup(cfg)
    finally:
        await site.stop()
        await runner.cleanup()

    # AgentskillsAdaptor copied the plugin skill into the canonical dir.
    assert (configs / "skills" / "probe-skill" / "SKILL.md").is_file()
    # Plugin rules fold into the ONE prompt channel hermes actually consumes.
    assert "ALWAYS-ON-PROBE-RULE" in (cfg.system_prompt or "")


@pytest.mark.asyncio
async def test_setup_wires_platform_mcp_into_hermes_native_config(
    monkeypatch, tmp_path
):
    """setup() must install an MCP-shaped platform plugin into the Hermes-native
    mcp_servers map. This pins the full adapter path:

      load_plugins(<configs>/plugins) -> install_plugins_via_registry()
      -> MCPServerAdaptor -> BaseAdapter.register_mcp_server_hook()
      -> ~/.hermes/config.yaml mcp_servers.<name>

    That is the runtime-visible gate the platform agent uses before coming
    online; writing only .claude/settings.json would reproduce the
    mcp_server_present=false failure.
    """

    monkeypatch.delenv("MOLECULE_SMOKE_MODE", raising=False)
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "false")
    monkeypatch.setenv("PLUGINS_DIR", str(tmp_path / "no-shared-plugins"))

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    hermes_cfg = home / ".hermes" / "config.yaml"
    hermes_cfg.parent.mkdir(parents=True)
    hermes_cfg.write_text(
        yaml.safe_dump(
            {
                "model": "nous:existing-model",
                "mcp_servers": {
                    "keep-me": {"command": "uvx", "args": ["keep-server"]}
                },
            },
            sort_keys=False,
        )
    )

    configs = tmp_path / "configs"
    plugin = configs / "plugins" / "molecule-platform-mcp"
    plugin.mkdir(parents=True)
    (plugin / "mcp-servers.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "molecule-platform": {
                        "command": "npx",
                        "args": ["-y", "@molecule-ai/mcp-server"],
                        "env": {"MOLECULE_MCP_MODE": "management"},
                    }
                }
            }
        )
    )

    api_port = _free_port()

    async def health_handler(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", api_port)
    await site.start()

    adapter = HermesAgentAdapter()
    try:
        monkeypatch.setenv(
            "HERMES_API_BASE", f"http://127.0.0.1:{api_port}/v1"
        )
        cfg = AdapterConfig(model="hermes-test", config_path=str(configs))
        await adapter.setup(cfg)
    finally:
        await site.stop()
        await runner.cleanup()

    parsed = yaml.safe_load(hermes_cfg.read_text())
    assert parsed["model"] == "nous:existing-model"
    assert parsed["mcp_servers"]["keep-me"] == {
        "command": "uvx",
        "args": ["keep-server"],
    }
    platform = parsed["mcp_servers"]["molecule-platform"]
    assert platform["command"] == "npx"
    assert platform["args"] == ["-y", "@molecule-ai/mcp-server"]
    assert platform["env"]["MOLECULE_MCP_MODE"] == "management"
    assert platform["env"]["WORKSPACE_ID"] == "ws-hermes-test"
    assert platform["env"]["PLATFORM_URL"] == "http://127.0.0.1:8080"
    assert not (configs / ".claude" / "settings.json").exists()
    assert adapter.management_mcp_present(cfg) is True


@pytest.mark.asyncio
async def test_smoke_mode_never_runs_plugin_installs(monkeypatch, tmp_path):
    """Smoke boots (publish-image gate: stub creds, no network, no plugins
    volume) must not execute plugin setup — the install call sits AFTER the
    smoke short-circuit."""

    monkeypatch.setenv("MOLECULE_SMOKE_MODE", "1")
    adapter = HermesAgentAdapter()

    async def _boom(*a, **kw):  # pragma: no cover - would fail the test
        raise AssertionError("install_plugins_via_registry ran under smoke mode")

    monkeypatch.setattr(adapter, "install_plugins_via_registry", _boom)
    cfg = AdapterConfig(model="hermes-test", config_path=str(tmp_path / "configs"))
    await adapter.setup(cfg)


# ---------------------------------------------------------------------------
# runtime#181 — hermes owns MCP-tool discovery (enumerate_loaded_mcp_tools).
# The RUNTIME CONTRACT default reads a .claude settings.json hermes doesn't
# have; hermes overrides it to read its own ~/.hermes/config.yaml mcp_servers
# block, so a concierge's stdio molecule-platform management MCP is enumerated
# and mcp__molecule-platform__provision_workspace reaches the first heartbeat.
# ---------------------------------------------------------------------------

def _write_hermes_config(home, servers):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}))


def test_read_hermes_mcp_servers_keeps_only_stdio(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    _write_hermes_config(
        home,
        {
            # url-transport a2a sidecar — NOT stdio-spawnable, must be skipped
            "molecule": {"url": "http://127.0.0.1:9100/mcp"},
            # stdio management MCP — the gate-relevant one
            "molecule-platform": {
                "command": "npx",
                "args": ["-y", "--prefer-offline", "@molecule-ai/mcp-server@1.8.2"],
                "env": {"MOLECULE_MCP_MODE": "management"},
            },
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    specs = HermesAgentAdapter._read_hermes_mcp_servers()
    assert list(specs) == ["molecule-platform"], "url sidecar must be dropped"
    assert specs["molecule-platform"]["command"] == "npx"


def test_read_hermes_mcp_servers_missing_file_returns_empty(monkeypatch, tmp_path):
    # non-concierge / pre-write: no config.yaml -> {} (never raises)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nope"))
    assert HermesAgentAdapter._read_hermes_mcp_servers() == {}


def test_read_hermes_mcp_servers_url_only_returns_empty(monkeypatch, tmp_path):
    # an ordinary tenant hermes: only the url a2a server, no admin token, no
    # molecule-platform. Nothing stdio-probable -> {} -> enumeration None.
    home = tmp_path / ".hermes"
    _write_hermes_config(home, {"molecule": {"url": "http://127.0.0.1:9100/mcp"}})
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert HermesAgentAdapter._read_hermes_mcp_servers() == {}


def test_read_hermes_mcp_servers_bad_yaml_returns_empty(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("mcp_servers: [this: is: not: valid")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert HermesAgentAdapter._read_hermes_mcp_servers() == {}


@pytest.mark.asyncio
async def test_enumerate_routes_stdio_specs_to_probe_engine(monkeypatch, tmp_path):
    """The override reads config.yaml and hands the stdio specs to the shared
    boot-safe probe engine (enumerate_from_specs_async) — returning the loaded
    tool ids the online gate keys on."""
    home = tmp_path / ".hermes"
    _write_hermes_config(
        home,
        {
            "molecule": {"url": "http://127.0.0.1:9100/mcp"},
            "molecule-platform": {
                "command": "npx",
                "args": ["-y", "@molecule-ai/mcp-server"],
                "env": {"MOLECULE_MCP_MODE": "management"},
            },
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Make the bundled node/npx resolvable so mcp_launch_env injects a PATH overlay.
    node_bin = home / "node" / "bin"
    node_bin.mkdir(parents=True)
    for exe in ("node", "npx"):
        (node_bin / exe).write_text("#!/bin/sh\n:\n")
        (node_bin / exe).chmod(0o755)

    captured = {}

    async def _fake_probe(servers, launch_env=None):
        captured["servers"] = servers
        captured["launch_env"] = launch_env
        return ["mcp__molecule-platform__provision_workspace"]

    import molecule_runtime.loaded_mcp_tools_probe as probe
    monkeypatch.setattr(probe, "enumerate_from_specs_async", _fake_probe)

    out = await HermesAgentAdapter().enumerate_loaded_mcp_tools(
        AdapterConfig(model="hermes-test")
    )
    assert out == ["mcp__molecule-platform__provision_workspace"]
    # only the stdio server reached the engine
    assert list(captured["servers"]) == ["molecule-platform"]
    # the dynamic launch-env overlay was threaded into the probe spawn, prepending
    # the bundled node bin dir to PATH (the off-PATH node/npx fix)
    assert str(node_bin) in captured["launch_env"]["PATH"].split(":")[0]


@pytest.mark.asyncio
async def test_enumerate_returns_none_when_no_stdio_server(monkeypatch, tmp_path):
    """A non-concierge hermes (url sidecar only) enumerates to None — the
    producer stays unset and core's grace window applies (no false tools)."""
    home = tmp_path / ".hermes"
    _write_hermes_config(home, {"molecule": {"url": "http://127.0.0.1:9100/mcp"}})
    monkeypatch.setenv("HERMES_HOME", str(home))

    async def _boom(servers, launch_env=None):  # pragma: no cover - must not be called
        raise AssertionError("probe engine must not run with no stdio specs")

    import molecule_runtime.loaded_mcp_tools_probe as probe
    monkeypatch.setattr(probe, "enumerate_from_specs_async", _boom)

    out = await HermesAgentAdapter().enumerate_loaded_mcp_tools(
        AdapterConfig(model="hermes-test")
    )
    assert out is None


# ---------------------------------------------------------------------------
# ADR-004 mcp_launch_env socket — DYNAMIC, adapter-resolved launch env.
#
# The live bug: the hermes image bundles Node 22 under $HERMES_HOME/node/bin but
# that dir is OFF the runtime process PATH, so a spawned `npx @molecule-ai/
# mcp-server` child can't resolve node/npx -> the management MCP never launches ->
# the concierge is stuck "provisioning". These prove the override resolves it
# DYNAMICALLY at launch (superseding the static Dockerfile PATH hardcode).
# ---------------------------------------------------------------------------


def _make_bundled_node(home):
    """Create fake bundled node/npx under ``home/node/bin`` and return the bin dir."""
    node_bin = home / "node" / "bin"
    node_bin.mkdir(parents=True)
    for exe in ("node", "npx"):
        (node_bin / exe).write_text("#!/bin/sh\n:\n")
        (node_bin / exe).chmod(0o755)
    return node_bin


def test_mcp_launch_env_prepends_bundled_node_bin_when_present(monkeypatch, tmp_path):
    """With node/npx bundled under $HERMES_HOME/node/bin (off the process PATH), the
    override returns a PATH overlay with that bin dir PREPENDED — the fix that makes
    the off-PATH interpreter resolvable for the spawned management MCP."""
    home = tmp_path / ".hermes"
    node_bin = _make_bundled_node(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # node bin NOT on it

    env = HermesAgentAdapter().mcp_launch_env(AdapterConfig(model="hermes-test"))

    assert "PATH" in env
    assert env["PATH"].split(":")[0] == str(node_bin)
    assert env["PATH"].endswith("/usr/bin:/bin")
    # dynamic resolution only — the process PATH itself is never mutated
    assert str(node_bin) not in os.environ["PATH"]


def test_mcp_launch_env_noop_when_no_bundled_node(monkeypatch, tmp_path):
    """No bundled node under $HERMES_HOME (a system-node image) -> {} no-op, so the
    spawned child inherits the process PATH unchanged (never a fake PATH claim)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert HermesAgentAdapter().mcp_launch_env(AdapterConfig(model="hermes-test")) == {}


def test_mcp_launch_env_noop_when_only_node_present_not_npx(monkeypatch, tmp_path):
    """Both node AND npx must be present — a partial install (node only) is treated
    as absent so we never inject a PATH that still can't launch npx-based servers."""
    home = tmp_path / ".hermes"
    node_bin = home / "node" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("#!/bin/sh\n:\n")
    (node_bin / "node").chmod(0o755)
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert HermesAgentAdapter().mcp_launch_env(AdapterConfig(model="hermes-test")) == {}


def test_mcp_launch_env_resolves_via_hermes_home_default_when_unset(monkeypatch, tmp_path):
    """HERMES_HOME unset -> resolves ~/.hermes (HOME-based), the SAME resolution the
    rest of the adapter uses, so node + MCP config + persona share one home."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    node_bin = _make_bundled_node(tmp_path / ".hermes")

    env = HermesAgentAdapter().mcp_launch_env(AdapterConfig(model="hermes-test"))
    assert env["PATH"].split(":")[0] == str(node_bin)
