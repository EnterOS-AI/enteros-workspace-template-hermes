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

import socket
from typing import Any

import pytest
from aiohttp import web

from adapter import HermesAgentAdapter, Adapter
from molecule_runtime.adapters.base import AdapterConfig


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
