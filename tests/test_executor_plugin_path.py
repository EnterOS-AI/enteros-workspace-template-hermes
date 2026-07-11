"""Tests for the hermes plugin transport in executor.py.

Coverage targets:
  - Lifecycle: start() boots reply server; stop() tears it down and
    fails any in-flight pending Futures.
  - Inbound: execute() POSTs to a stub /a2a/inbound, registers a
    Future keyed by message_id, awaits it.
  - Outbound: a stub plugin POSTs to our reply server with reply_to
    matching the inbound message_id; the awaiting execute() resolves
    and emits on the event_queue.
  - Auth: shared_secret is sent on outbound POST and required on
    inbound reply POST.
  - Error paths: empty prompt, hermes-side POST failure, reply timeout,
    late/duplicate replies for unknown message_id.
  - Fallback: when MOLECULE_A2A_PLATFORM_ENABLED=false the executor
    falls through to the chat_completions transport.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import httpx
import pytest
from aiohttp import ClientSession, ClientTimeout, web

from executor import HermesAgentProxyExecutor, SECRET_HEADER
import executor as executor_mod
from molecule_runtime.adapters.base import AdapterConfig


# ---- helpers --------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_executor(monkeypatch, **env: str) -> HermesAgentProxyExecutor:
    """Test helper. Defaults MOLECULE_A2A_PLATFORM_ENABLED=true so the
    plugin-path tests below operate in plugin mode by default. Tests
    that exercise the chat_completions fallback override this.

    WORKSPACE_ID is explicitly UNSET by default so existing tests
    continue to exercise the context_id/task_id fallback chain in
    _derive_chat_id. Tests that need the workspace_id-first hot-patch
    path (PR #37, task #262) set WORKSPACE_ID via env= kwarg."""
    env.setdefault("MOLECULE_A2A_PLATFORM_ENABLED", "true")
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = AdapterConfig(
        model="hermes-test",
        system_prompt="you are helpful",
    )
    return HermesAgentProxyExecutor(cfg)


class _CapturingQueue:
    """Stand-in for a2a.server.events.EventQueue."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


def _build_context(text: str, *, task_id: str = "task-1"):
    """A minimal RequestContext stub the executor's helper logic
    can extract text + task_id from. The real a2a-sdk class has many
    fields; we only need the ones executor.py touches."""
    ctx = MagicMock()
    ctx.task_id = task_id
    ctx.session_id = None
    ctx.context_id = None
    msg = MagicMock()
    msg.task_id = task_id
    # Stand up a `parts` list of `TextPart`-shaped objects so
    # extract_message_text(msg) returns our prompt.
    text_part = MagicMock()
    text_part.text = text
    text_part.kind = "text"
    msg.parts = [text_part]
    ctx.message = msg
    return ctx


# ---- structural -----------------------------------------------------


def test_executor_init_defaults_to_chat_completions(monkeypatch):
    """Default is the safe legacy /v1/chat/completions transport while
    the image-side plugin install is being debugged. Plugin path is
    opt-in via MOLECULE_A2A_PLATFORM_ENABLED=true. Port defaults still
    apply for when the plugin path is enabled.

    Built without _make_executor so the helper's plugin-on default
    doesn't mask the prod default we want to assert here."""
    monkeypatch.delenv("MOLECULE_A2A_PLATFORM_ENABLED", raising=False)
    cfg = AdapterConfig(model="hermes-test", system_prompt="you are helpful")
    ex = HermesAgentProxyExecutor(cfg)
    assert ex._use_plugin is False
    # Defaults still set so plugin enable just needs the one env flip.
    assert ex._plugin_port == 8645
    assert ex._callback_port == 8646


def test_executor_enables_plugin_path_when_opted_in(monkeypatch):
    ex = _make_executor(monkeypatch, MOLECULE_A2A_PLATFORM_ENABLED="true")
    assert ex._use_plugin is True


def test_executor_disabled_explicitly(monkeypatch):
    ex = _make_executor(monkeypatch, MOLECULE_A2A_PLATFORM_ENABLED="false")
    assert ex._use_plugin is False


def test_executor_respects_port_overrides(monkeypatch):
    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT="9999",
        MOLECULE_A2A_CALLBACK_PORT="9000",
    )
    assert ex._plugin_port == 9999
    assert ex._callback_port == 9000


def test_molecule_tools_come_from_runtime_contract():
    from molecule_runtime.mcp_tools import openai_function_tools

    assert executor_mod._ACTIVE_TOOLS == openai_function_tools()
    assert not hasattr(executor_mod, "_MOLECULE_TOOLS")


@pytest.mark.asyncio
async def test_dispatch_tool_uses_runtime_dispatcher(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_dispatch(name: str, arguments: dict[str, Any]) -> str:
        calls.append((name, arguments))
        return "shared result"

    monkeypatch.setattr(executor_mod, "handle_molecule_tool_call", fake_dispatch)
    ex = _make_executor(monkeypatch)

    result = await ex._dispatch_tool({
        "function": {
            "name": "delegate_task",
            "arguments": '{"workspace_id":"ws-1","task":"hello"}',
        }
    })

    assert result == "shared result"
    assert calls == [("delegate_task", {"workspace_id": "ws-1", "task": "hello"})]


@pytest.mark.asyncio
async def test_dispatch_tool_emits_tool_call(monkeypatch):
    """ADR-004: the in-process tool loop MUST emit a tool-call activity row via
    the engine-owned emit_tool_call primitive so the canvas renders a
    ToolTraceChip. Assert _dispatch_tool calls emit_tool_call with the tool
    name and the engine's summary, and that it fires even if the underlying
    dispatch later errors (emit is at the tool SITE, before dispatch)."""
    emits: list[tuple[str, str]] = []

    async def fake_emit(name, summary=None, status="ok"):
        emits.append((name, summary))

    async def fake_dispatch(name, arguments):
        return "ok"

    monkeypatch.setattr(executor_mod, "emit_tool_call", fake_emit)
    monkeypatch.setattr(executor_mod, "handle_molecule_tool_call", fake_dispatch)
    ex = _make_executor(monkeypatch)

    await ex._dispatch_tool({
        "function": {
            "name": "delegate_task",
            "arguments": '{"workspace_id":"ws-1","task":"hello"}',
        }
    })

    assert len(emits) == 1
    emitted_name, emitted_summary = emits[0]
    assert emitted_name == "delegate_task"
    # Engine generic summary is "🛠 <name>(…)"; assert the tool name rides in
    # the summary rather than pinning the exact emoji/format the engine owns.
    assert "delegate_task" in emitted_summary


@pytest.mark.asyncio
async def test_dispatch_tool_emits_even_when_dispatch_errors(monkeypatch):
    """The emit is at the tool SITE (before dispatch), so the ToolTraceChip
    renders even when the tool itself errors — the failure is surfaced as the
    tool's returned error string, not by suppressing the chip."""
    emits: list[str] = []

    async def fake_emit(name, summary=None, status="ok"):
        emits.append(name)

    async def boom_dispatch(name, arguments):
        raise RuntimeError("tool blew up")

    monkeypatch.setattr(executor_mod, "emit_tool_call", fake_emit)
    monkeypatch.setattr(executor_mod, "handle_molecule_tool_call", boom_dispatch)
    ex = _make_executor(monkeypatch)

    result = await ex._dispatch_tool({
        "function": {"name": "delegate_task", "arguments": "{}"},
    })

    # dispatch error is caught + returned as a string (existing behavior)…
    assert "Tool error (delegate_task)" in result
    # …and the chip was still emitted for the attempted call.
    assert emits == ["delegate_task"]


# ---- lifecycle ------------------------------------------------------


@pytest.mark.asyncio
async def test_start_boots_reply_server_and_stop_tears_it_down(monkeypatch):
    cb_port = _free_port()
    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
    )
    try:
        await ex.start()
        # Server is up — POSTing should reach the handler (and fail
        # validation since no reply_to/content; but the connection
        # itself proves the listener exists).
        async with ClientSession(timeout=ClientTimeout(total=2)) as s:
            async with s.post(
                f"http://127.0.0.1:{cb_port}/a2a/reply", json={}
            ) as r:
                assert r.status == 400
    finally:
        await ex.stop()
    # After stop, connection should refuse.
    async with ClientSession(timeout=ClientTimeout(total=1)) as s:
        with pytest.raises(Exception):
            async with s.post(
                f"http://127.0.0.1:{cb_port}/a2a/reply", json={}
            ) as r:
                pass


@pytest.mark.asyncio
async def test_start_idempotent(monkeypatch):
    cb_port = _free_port()
    ex = _make_executor(monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(cb_port))
    try:
        await ex.start()
        await ex.start()  # should be a no-op, not a port-in-use error
    finally:
        await ex.stop()


@pytest.mark.asyncio
async def test_stop_fails_pending_futures(monkeypatch):
    ex = _make_executor(
        monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(_free_port())
    )
    await ex.start()
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    ex._pending["msg-1"] = fut
    await ex.stop()
    assert fut.done()
    with pytest.raises(RuntimeError, match="executor shutting down"):
        fut.result()
    assert ex._pending == {}


# ---- happy path -----------------------------------------------------


@pytest.mark.asyncio
async def test_execute_via_plugin_round_trips(monkeypatch):
    """Stand up a fake hermes plugin that ack's the inbound POST and
    asynchronously POSTs back a reply matched by reply_to. Confirm
    execute() emits the reply text on the event queue."""

    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_received: List[Dict[str, Any]] = []

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_received.append({
            "headers": dict(request.headers),
            "body": body,
        })
        # Simulate hermes processing — POST a reply back to the
        # callback URL the executor sent us.
        async def _delayed_reply():
            await asyncio.sleep(0.05)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                await s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "hello back from hermes",
                        "reply_to": body["message_id"],
                        "metadata": {},
                    },
                )
        asyncio.create_task(_delayed_reply())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        ctx = _build_context("hello hermes")
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(inbound_received) == 1
    assert inbound_received[0]["body"]["content"] == "hello hermes"
    assert inbound_received[0]["body"]["chat_id"] == "task-1"
    assert "callback_url" in inbound_received[0]["body"]
    assert "message_id" in inbound_received[0]["body"]

    assert len(queue.events) == 1
    # The event is the a2a-sdk new_text_message envelope; confirm the
    # text we set survived the round trip somewhere in its repr.
    repr_event = repr(queue.events[0])
    assert "hello back from hermes" in repr_event


@pytest.mark.asyncio
async def test_execute_empty_prompt_surfaces_actionable_reason(monkeypatch):
    """Phase 1 file-only message support (a1ea2200): empty text AND no
    files → actionable user-facing reason, NOT the old opaque
    "(empty prompt — nothing to do)" string.

    Per feedback_surface_actionable_failure_reason_to_user — the user
    must be able to see WHY their turn produced nothing.
    """
    ex = _make_executor(
        monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(_free_port())
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        ctx = _build_context("   ")  # whitespace only
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()

    assert len(queue.events) == 1
    rendered = repr(queue.events[0])
    assert "Your message was empty" in rendered
    assert "send text or a file" in rendered
    # The old opaque string must NOT appear.
    assert "(empty prompt — nothing to do)" not in rendered


def _build_context_with_file(
    *, name: str, mime_type: str, path: str, text: str = "",
    task_id: str = "task-file-only",
):
    """RequestContext stub with an attached FilePart (v0-flat shape).

    Mirrors _build_context() but adds a file part. Pass ``text=""`` for
    a true file-only message.
    """
    ctx = MagicMock()
    ctx.task_id = task_id
    ctx.session_id = None
    ctx.context_id = None
    msg = MagicMock()
    msg.task_id = task_id
    parts = []
    if text:
        text_part = MagicMock()
        text_part.text = text
        text_part.kind = "text"
        parts.append(text_part)
    file_part = MagicMock(spec=["kind", "root", "file", "url", "filename", "media_type"])
    file_part.kind = "file"
    file_part.root = file_part  # extract_attached_files uses .root or part itself
    # Suppress .text so extract_message_text doesn't trip on a MagicMock value.
    del file_part.text  # type: ignore[attr-defined]
    file_obj = MagicMock(spec=["uri", "name", "mimeType", "mime_type"])
    file_obj.uri = f"file://{path}"
    file_obj.name = name
    file_obj.mimeType = mime_type
    file_part.file = file_obj
    parts.append(file_part)
    msg.parts = parts
    ctx.message = msg
    return ctx


@pytest.mark.asyncio
async def test_execute_file_only_no_longer_returns_opaque_empty(
    monkeypatch, tmp_path,
):
    """File-only message must NOT short-circuit with the opaque
    "(empty prompt — nothing to do)" string.

    The fix passes the file manifest through to the plugin transport,
    so we stub _execute_via_plugin and assert it received a prompt that
    names the attached file (no event short-circuit).
    """
    import molecule_runtime.executor_helpers as _helpers
    monkeypatch.setattr(_helpers, "WORKSPACE_MOUNT", str(tmp_path))

    pdf = tmp_path / "chloe.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub\n")

    ex = _make_executor(
        monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(_free_port())
    )
    queue = _CapturingQueue()

    captured: List[str] = []

    async def fake_plugin(context, event_queue, prompt, history=None):  # type: ignore[no-untyped-def]
        captured.append(prompt)

    ex._execute_via_plugin = fake_plugin  # type: ignore[assignment,method-assign]

    try:
        await ex.start()
        ctx = _build_context_with_file(
            name="chloe.pdf",
            mime_type="application/pdf",
            path=str(pdf),
        )
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()

    blob = repr(queue.events) + repr(captured)
    assert "empty prompt — nothing to do" not in blob
    assert any("chloe.pdf" in p for p in captured), (
        f"plugin transport never saw the file manifest; captured={captured!r}"
    )


@pytest.mark.asyncio
async def test_execute_text_only_still_passes_prompt_unchanged(monkeypatch):
    """Regression-pin: text-only messages keep working exactly as
    before — the file-aware branch must not perturb the text path."""
    ex = _make_executor(
        monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(_free_port())
    )
    queue = _CapturingQueue()

    captured: List[str] = []

    async def fake_plugin(context, event_queue, prompt, history=None):  # type: ignore[no-untyped-def]
        captured.append(prompt)

    ex._execute_via_plugin = fake_plugin  # type: ignore[assignment,method-assign]

    try:
        await ex.start()
        ctx = _build_context("write a haiku")
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()

    assert captured == ["write a haiku"]


# ---- error paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_execute_handles_inbound_post_failure(monkeypatch):
    """No plugin listening at the configured port — execute() should
    emit an error message rather than hang."""

    closed_port = _free_port()  # nothing binds to it
    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(closed_port),
        MOLECULE_A2A_CALLBACK_PORT=str(_free_port()),
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        ctx = _build_context("anybody home")
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()

    assert len(queue.events) == 1
    repr_event = repr(queue.events[0])
    assert "hermes plugin POST error" in repr_event
    # No leaked pending entry.
    assert ex._pending == {}


@pytest.mark.asyncio
async def test_execute_handles_reply_timeout(monkeypatch):
    """Inbound POST succeeds but no reply comes — execute() emits a
    timeout message after the configured deadline."""

    plugin_port = _free_port()

    async def silent_inbound(request: web.Request) -> web.Response:
        await request.json()
        return web.json_response({"ok": True, "queued": True})  # no callback

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", silent_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    # Patch the timeout so the test runs in <1s instead of 600s.
    import executor as ex_mod
    monkeypatch.setattr(ex_mod, "_PLUGIN_REPLY_TIMEOUT", 0.5)

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(_free_port()),
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        ctx = _build_context("waiting for nothing")
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(queue.events) == 1
    assert "reply timeout" in repr(queue.events[0])
    assert ex._pending == {}


# ---- shared-secret enforcement --------------------------------------


@pytest.mark.asyncio
async def test_shared_secret_sent_on_inbound_and_required_on_reply(monkeypatch):
    """When MOLECULE_A2A_PLATFORM_SHARED_SECRET is set, the executor
    must include the X-Molecule-A2A-Secret header on outbound POSTs
    and require it on incoming reply POSTs."""

    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_headers: Dict[str, str] = {}

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_headers.update(dict(request.headers))
        # Reply WITHOUT the secret header — should be rejected 401.
        async def _bad_then_good():
            await asyncio.sleep(0.02)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                # First: no header → 401 (and the future stays pending).
                async with s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "should be rejected",
                        "reply_to": body["message_id"],
                    },
                ) as r1:
                    assert r1.status == 401
                # Second: with header → 200, future resolves.
                await s.post(
                    body["callback_url"],
                    headers={SECRET_HEADER: "topsecret"},
                    json={
                        "chat_id": body["chat_id"],
                        "content": "authorized reply",
                        "reply_to": body["message_id"],
                    },
                )
        asyncio.create_task(_bad_then_good())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
        MOLECULE_A2A_PLATFORM_SHARED_SECRET="topsecret",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(_build_context("auth me"), queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert inbound_headers.get(SECRET_HEADER) == "topsecret"
    assert "authorized reply" in repr(queue.events[0])


@pytest.mark.asyncio
async def test_reply_for_unknown_message_id_acks_stale(monkeypatch):
    """A reply for a message_id we don't have pending should ack
    {ok: true, stale: true} so the plugin doesn't retry forever."""

    cb_port = _free_port()
    ex = _make_executor(monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(cb_port))
    try:
        await ex.start()
        async with ClientSession(timeout=ClientTimeout(total=2)) as s:
            async with s.post(
                f"http://127.0.0.1:{cb_port}/a2a/reply",
                json={"reply_to": "ghost-id", "content": "anybody home"},
            ) as r:
                assert r.status == 200
                body = await r.json()
                assert body == {"ok": True, "stale": True}
    finally:
        await ex.stop()


@pytest.mark.asyncio
async def test_reply_validates_required_fields(monkeypatch):
    cb_port = _free_port()
    ex = _make_executor(monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(cb_port))
    try:
        await ex.start()
        async with ClientSession(timeout=ClientTimeout(total=2)) as s:
            # Missing both
            async with s.post(
                f"http://127.0.0.1:{cb_port}/a2a/reply", json={}
            ) as r:
                assert r.status == 400
            # Missing content
            async with s.post(
                f"http://127.0.0.1:{cb_port}/a2a/reply",
                json={"reply_to": "x"},
            ) as r:
                assert r.status == 400
            # Bad JSON
            async with s.post(
                f"http://127.0.0.1:{cb_port}/a2a/reply",
                data="not json",
            ) as r:
                assert r.status == 400
    finally:
        await ex.stop()


# ---- chat_completions fallback --------------------------------------


@pytest.mark.asyncio
async def test_fallback_chat_completions_path(monkeypatch):
    """With MOLECULE_A2A_PLATFORM_ENABLED=false, the executor must POST
    to /v1/chat/completions and emit the assistant text."""

    api_port = _free_port()

    async def fake_chat_completions(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["model"] == "hermes-test"
        return web.json_response({
            "choices": [
                {"message": {"role": "assistant", "content": "fallback reply"}}
            ]
        })

    api_app = web.Application()
    api_app.router.add_post("/v1/chat/completions", fake_chat_completions)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", api_port)
    await api_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_ENABLED="false",
        HERMES_API_BASE=f"http://127.0.0.1:{api_port}/v1",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()  # no-op when plugin disabled
        await ex.execute(_build_context("legacy"), queue)
    finally:
        await ex.stop()
        await api_site.stop()
        await api_runner.cleanup()

    assert len(queue.events) == 1
    assert "fallback reply" in repr(queue.events[0])


@pytest.mark.asyncio
async def test_fallback_handles_chat_completions_http_error(monkeypatch):
    api_port = _free_port()

    async def err500(request: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    api_app = web.Application()
    api_app.router.add_post("/v1/chat/completions", err500)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", api_port)
    await api_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_ENABLED="false",
        HERMES_API_BASE=f"http://127.0.0.1:{api_port}/v1",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(_build_context("trigger 500"), queue)
    finally:
        await ex.stop()
        await api_site.stop()
        await api_runner.cleanup()

    assert "hermes-agent error 500" in repr(queue.events[0])


@pytest.mark.asyncio
async def test_fallback_handles_unreachable_chat_completions(monkeypatch):
    closed_port = _free_port()
    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_ENABLED="false",
        HERMES_API_BASE=f"http://127.0.0.1:{closed_port}/v1",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(_build_context("nowhere"), queue)
    finally:
        await ex.stop()

    assert "hermes-agent unreachable" in repr(queue.events[0])


@pytest.mark.asyncio
async def test_fallback_handles_unexpected_response_shape(monkeypatch):
    api_port = _free_port()

    async def junk_response(request: web.Request) -> web.Response:
        return web.json_response({"not": "what we expected"})

    api_app = web.Application()
    api_app.router.add_post("/v1/chat/completions", junk_response)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", api_port)
    await api_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_ENABLED="false",
        HERMES_API_BASE=f"http://127.0.0.1:{api_port}/v1",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(_build_context("junk"), queue)
    finally:
        await ex.stop()
        await api_site.stop()
        await api_runner.cleanup()

    assert "no content" in repr(queue.events[0])


# ---- reasoning-model content extraction (#2204) ----------------------


def test_extract_assistant_text_returns_content_when_present():
    """Regression-pin: a normal completion with non-empty content is
    returned verbatim — the reasoning fallback must not perturb it."""
    data = {"choices": [{"message": {"role": "assistant", "content": "PONG"}}]}
    assert HermesAgentProxyExecutor._extract_assistant_text(data) == "PONG"


def test_extract_assistant_text_falls_back_to_reasoning_content():
    """#2204: reasoning models (MiniMax M2/M2.7, Moonshot K2.6) put the
    turn in ``reasoning_content`` and leave ``content`` empty when the
    whole budget went to reasoning. The extractor must surface that
    rather than reporting an empty turn."""
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "Let me think... the answer is PONG.",
                }
            }
        ]
    }
    assert (
        HermesAgentProxyExecutor._extract_assistant_text(data)
        == "Let me think... the answer is PONG."
    )


def test_extract_assistant_text_prefers_content_over_reasoning():
    """When BOTH content and reasoning_content are present, content wins
    — the reasoning preamble is not the final answer."""
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "PONG",
                    "reasoning_content": "internal scratchpad",
                }
            }
        ]
    }
    assert HermesAgentProxyExecutor._extract_assistant_text(data) == "PONG"


def test_extract_assistant_text_null_content_with_reasoning():
    """Some providers send content: null (not "") on a reasoning-only
    turn. Treat null the same as empty and fall back to reasoning."""
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "reasoned answer",
                }
            }
        ]
    }
    assert (
        HermesAgentProxyExecutor._extract_assistant_text(data)
        == "reasoned answer"
    )


def test_extract_assistant_text_both_empty_still_flagged():
    """Genuine empty/error reply — content AND reasoning_content both
    empty — must still return the sentinel, NOT a blank string. We do
    NOT mask a truly empty turn as if it were a reasoning-only one."""
    data = {
        "choices": [
            {"message": {"role": "assistant", "content": "", "reasoning_content": ""}}
        ]
    }
    assert (
        HermesAgentProxyExecutor._extract_assistant_text(data)
        == "(hermes-agent returned no content)"
    )


def test_extract_assistant_text_bad_shape_flagged():
    """A malformed response (no choices/message) still returns the
    sentinel rather than raising."""
    assert (
        HermesAgentProxyExecutor._extract_assistant_text({"not": "expected"})
        == "(hermes-agent returned no content)"
    )


@pytest.mark.asyncio
async def test_fallback_surfaces_reasoning_only_completion(monkeypatch):
    """End-to-end on the chat_completions path: an upstream that returns
    a 2xx with empty content but populated reasoning_content (the staging
    canary failure on MiniMax-M2 / kimi-k2.6) must emit the reasoning
    text on the queue, NOT the empty-content sentinel."""
    api_port = _free_port()

    async def reasoning_only(request: web.Request) -> web.Response:
        return web.json_response({
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "PONG",
                    },
                }
            ]
        })

    api_app = web.Application()
    api_app.router.add_post("/v1/chat/completions", reasoning_only)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", api_port)
    await api_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_ENABLED="false",
        HERMES_API_BASE=f"http://127.0.0.1:{api_port}/v1",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(_build_context("ping"), queue)
    finally:
        await ex.stop()
        await api_site.stop()
        await api_runner.cleanup()

    assert len(queue.events) == 1
    rendered = repr(queue.events[0])
    assert "PONG" in rendered
    assert "no content" not in rendered


# ---- chat_id derivation ----------------------------------------------


def test_derive_chat_id_prefers_context_id_over_task_id(monkeypatch):
    """Load-bearing assertion: when WORKSPACE_ID is unset and both
    context_id and task_id are populated, chat_id must derive from
    context_id (PR #35 priority chain, preserved as PR #37 fallback).
    Mirrors workspace-template-openclaw PR #29's
    test_session_id_prefers_context_id_over_task_id."""
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    ctx = MagicMock()
    ctx.context_id = "chat-stable"
    ctx.task_id = "task-changes-per-turn"
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "chat-stable"


def test_derive_chat_id_falls_back_to_session_id_when_context_id_unset(
    monkeypatch,
):
    """Backwards-compat (WORKSPACE_ID unset): when context_id is absent
    but session_id is set, chat_id derives from session_id."""
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    ctx = MagicMock()
    ctx.context_id = None
    ctx.session_id = "sess-B"
    ctx.task_id = "task-C"
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "sess-B"


def test_derive_chat_id_falls_back_to_task_id_when_context_and_session_unset(
    monkeypatch,
):
    """Backwards-compat (WORKSPACE_ID unset) for older a2a-sdk shapes
    that don't populate context_id or session_id. task_id is the last
    context-level fallback before message-attr probing."""
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    ctx = MagicMock()
    ctx.context_id = None
    ctx.session_id = None
    ctx.task_id = "task-only"
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "task-only"


def test_derive_chat_id_synthesizes_when_nothing_present(monkeypatch):
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    ctx = MagicMock()
    ctx.context_id = None
    ctx.session_id = None
    ctx.task_id = None
    ctx.message = None
    derived = HermesAgentProxyExecutor._derive_chat_id(ctx)
    assert derived.startswith("adhoc-")


def test_derive_chat_id_falls_back_to_message_context_id(monkeypatch):
    """When WORKSPACE_ID + ctx.{context_id,session_id,task_id} are all
    unset but ctx.message has context_id, derive_chat_id uses that."""
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    ctx = MagicMock()
    ctx.context_id = None
    ctx.session_id = None
    ctx.task_id = None
    msg = MagicMock()
    msg.context_id = "ctx-from-message"
    msg.session_id = None
    msg.task_id = "task-from-message"
    ctx.message = msg
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "ctx-from-message"


def test_derive_chat_id_uses_message_id_camelcase(monkeypatch):
    """The a2a-sdk uses messageId on the Message object — ensure we
    pick it up as a last fallback before synthesizing an adhoc id."""
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    ctx = MagicMock()
    ctx.context_id = None
    ctx.session_id = None
    ctx.task_id = None
    msg = MagicMock()
    msg.context_id = None
    msg.session_id = None
    msg.task_id = None
    msg.messageId = "msg-camelcase"
    ctx.message = msg
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "msg-camelcase"


@pytest.mark.asyncio
async def test_cancel_returns_none():
    """cancel() is a noop today — confirm it doesn't raise and returns
    None so a2a-sdk's contract is satisfied."""
    cfg = AdapterConfig(model="hermes-test")
    ex = HermesAgentProxyExecutor(cfg)
    result = await ex.cancel(MagicMock(), _CapturingQueue())
    assert result is None


@pytest.mark.asyncio
async def test_plugin_path_resolves_with_runtime_error(monkeypatch):
    """If something inside the awaited future raises a non-timeout
    exception (e.g., the executor was stopped mid-flight), execute()
    should emit '[hermes plugin error] ...' rather than propagate."""

    plugin_port = _free_port()

    async def fake_inbound(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "queued": True})  # no callback

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(_free_port()),
    )
    queue = _CapturingQueue()
    try:
        await ex.start()

        # Race: send the request, then concurrently fail every pending
        # future with a custom RuntimeError.
        async def _execute():
            await ex.execute(_build_context("simulate failure"), queue)

        async def _trip():
            # Wait for execute() to register its pending future.
            for _ in range(50):
                if ex._pending:
                    break
                await asyncio.sleep(0.01)
            for fut in list(ex._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("simulated mid-flight failure"))

        await asyncio.gather(_execute(), _trip())
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert "hermes plugin error" in repr(queue.events[0])
    assert "simulated mid-flight failure" in repr(queue.events[0])


@pytest.mark.asyncio
async def test_chat_completions_includes_bearer_when_key_set(monkeypatch):
    """When API_SERVER_KEY is set, the legacy fallback must add an
    Authorization header to /v1/chat/completions calls."""

    api_port = _free_port()
    received_headers: Dict[str, str] = {}

    async def fake_chat_completions(request: web.Request) -> web.Response:
        received_headers.update(dict(request.headers))
        return web.json_response({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        })

    api_app = web.Application()
    api_app.router.add_post("/v1/chat/completions", fake_chat_completions)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", api_port)
    await api_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_ENABLED="false",
        HERMES_API_BASE=f"http://127.0.0.1:{api_port}/v1",
        API_SERVER_KEY="test-bearer-token",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(_build_context("authed"), queue)
    finally:
        await ex.stop()
        await api_site.stop()
        await api_runner.cleanup()

    assert received_headers.get("Authorization") == "Bearer test-bearer-token"


@pytest.mark.asyncio
async def test_plugin_path_reply_handler_exception_path(monkeypatch):
    """If something inside our reply handler raises (e.g., the future
    is cancelled mid-set), we should log and not crash the executor."""

    cb_port = _free_port()
    ex = _make_executor(monkeypatch, MOLECULE_A2A_CALLBACK_PORT=str(cb_port))
    try:
        await ex.start()
        # Pre-resolve a future then send a reply for it — the handler
        # tries to call set_result on a done future. We guard against
        # that with the not future.done() check, so this should ack OK.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        fut.set_result("already-done")
        ex._pending["pre-resolved"] = fut

        async with ClientSession(timeout=ClientTimeout(total=2)) as s:
            async with s.post(
                f"http://127.0.0.1:{cb_port}/a2a/reply",
                json={
                    "reply_to": "pre-resolved",
                    "content": "late delivery",
                },
            ) as r:
                assert r.status == 200
                body = await r.json()
                assert body == {"ok": True}
    finally:
        await ex.stop()


# ---- pyproject-style configuration -----------------------------------


def test_pytest_can_collect_module():
    """Trivial sanity check that conftest.py wires sys.path correctly
    and the test module imports cleanly without monkeypatch fixtures."""
    import executor  # noqa: F401
    assert hasattr(executor, "HermesAgentProxyExecutor")


# ---- session continuity: canvas history prepend ----------------------


def _build_context_with_history(text: str, history, *, task_id: str = "task-1"):
    """Variant of _build_context that also attaches metadata.history,
    the shape canvas/src/components/tabs/chat/hooks/useChatSend.ts:104-111
    actually ships."""
    ctx = MagicMock()
    ctx.task_id = task_id
    ctx.session_id = None
    ctx.context_id = None
    msg = MagicMock()
    msg.task_id = task_id
    text_part = MagicMock()
    text_part.text = text
    text_part.kind = "text"
    msg.parts = [text_part]
    msg.metadata = {"history": history}
    ctx.message = msg
    return ctx


def test_build_initial_messages_with_history_prepends_prior_turns(monkeypatch):
    """Forensic a819052e: hermes was building [system, user] every turn.
    The fix prepends translated prior turns between them so the LLM sees
    [system, ...history, user]."""
    cfg = AdapterConfig(model="hermes-test", system_prompt="be helpful")
    ex = HermesAgentProxyExecutor(cfg)

    history = [
        {"role": "user", "parts": [{"kind": "text", "text": "Hi, my name is Hongming."}]},
        {"role": "agent", "parts": [{"kind": "text", "text": "Hello Hongming!"}]},
    ]
    messages = ex._build_initial_messages("What is my name?", history=history)

    # Expect: [system, user(prior), assistant(prior), user(current)]
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "Hi, my name is Hongming."}
    assert messages[2] == {"role": "assistant", "content": "Hello Hongming!"}
    assert messages[3] == {"role": "user", "content": "What is my name?"}


def test_build_initial_messages_role_translation_agent_to_assistant(monkeypatch):
    """Canvas uses role='agent' for assistant turns; chat-completions
    requires role='assistant'. Unknown roles must be skipped (not
    smuggled in as something the LLM will misinterpret)."""
    cfg = AdapterConfig(model="hermes-test", system_prompt="")
    ex = HermesAgentProxyExecutor(cfg)

    history = [
        {"role": "user", "parts": [{"kind": "text", "text": "u1"}]},
        {"role": "agent", "parts": [{"kind": "text", "text": "a1"}]},
        {"role": "system", "parts": [{"kind": "text", "text": "bogus"}]},
        {"role": "tool", "parts": [{"kind": "text", "text": "alsobogus"}]},
        {"role": "user", "parts": [{"kind": "text", "text": "u2"}]},
    ]
    messages = ex._build_initial_messages("current", history=history)

    # No system prompt set → first message is the first history turn.
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "user"]
    contents = [m["content"] for m in messages]
    assert contents == ["u1", "a1", "u2", "current"]


def test_build_initial_messages_no_history_matches_legacy_shape(monkeypatch):
    """Regression guard: with history=None, behavior is identical to the
    pre-fix [system, user] shape so callers that don't pass history (e.g.
    peer-agent A2A turns with no metadata.history) aren't disturbed."""
    cfg = AdapterConfig(model="hermes-test", system_prompt="be helpful")
    ex = HermesAgentProxyExecutor(cfg)

    messages = ex._build_initial_messages("hello", history=None)
    assert messages == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hello"},
    ]


def test_build_initial_messages_tolerates_string_content_in_history():
    """If a peer pre-flattens parts into a content string, accept it
    rather than dropping the turn on the floor."""
    cfg = AdapterConfig(model="hermes-test", system_prompt="")
    ex = HermesAgentProxyExecutor(cfg)

    history = [
        {"role": "user", "content": "string-form"},
        {"role": "agent", "content": "also-string"},
    ]
    messages = ex._build_initial_messages("now", history=history)
    assert messages == [
        {"role": "user", "content": "string-form"},
        {"role": "assistant", "content": "also-string"},
        {"role": "user", "content": "now"},
    ]


def test_extract_history_from_context_reads_message_metadata():
    """The history lives at context.message.metadata.history — confirm
    the extractor finds it there and not elsewhere."""
    history = [{"role": "user", "parts": [{"kind": "text", "text": "x"}]}]
    ctx = _build_context_with_history("hi", history)
    assert HermesAgentProxyExecutor._extract_history_from_context(ctx) == history


def test_extract_history_from_context_handles_missing_metadata():
    """Peer-agent A2A turns and old canvas builds may ship without
    metadata.history — extractor returns [] not raises."""
    ctx = MagicMock()
    msg = MagicMock()
    msg.metadata = None
    ctx.message = msg
    assert HermesAgentProxyExecutor._extract_history_from_context(ctx) == []


@pytest.mark.asyncio
async def test_execute_via_chat_completions_extracts_history_from_metadata(
    monkeypatch,
):
    """End-to-end of the fallback path: the executor must read
    metadata.history off the incoming A2A request and emit
    [system, ...history, user] in the chat-completions POST body."""
    api_port = _free_port()
    captured_bodies: List[Dict[str, Any]] = []

    async def fake_chat_completions(request: web.Request) -> web.Response:
        captured_bodies.append(await request.json())
        return web.json_response({
            "choices": [
                {"message": {"role": "assistant", "content": "ack"}}
            ]
        })

    api_app = web.Application()
    api_app.router.add_post("/v1/chat/completions", fake_chat_completions)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", api_port)
    await api_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_ENABLED="false",
        HERMES_API_BASE=f"http://127.0.0.1:{api_port}/v1",
    )
    queue = _CapturingQueue()
    history = [
        {"role": "user", "parts": [{"kind": "text", "text": "turn1-u"}]},
        {"role": "agent", "parts": [{"kind": "text", "text": "turn1-a"}]},
    ]
    try:
        await ex.start()
        await ex.execute(
            _build_context_with_history("turn2-u", history), queue
        )
    finally:
        await ex.stop()
        await api_site.stop()
        await api_runner.cleanup()

    assert len(captured_bodies) == 1
    sent = captured_bodies[0]["messages"]
    # Expect [system, user(turn1), assistant(turn1), user(turn2)]
    assert [m["role"] for m in sent] == [
        "system", "user", "assistant", "user",
    ]
    assert sent[1]["content"] == "turn1-u"
    assert sent[2]["content"] == "turn1-a"
    assert sent[3]["content"] == "turn2-u"


@pytest.mark.asyncio
async def test_executor_plugin_payload_includes_messages_history(monkeypatch):
    """Belt-and-suspenders (task #385): the executor MUST forward
    canvas-shipped metadata.history as ``messages_history`` on the
    plugin path so the in-container plugin adapter can seed the daemon
    transcript on a fresh session.

    Reverses the prior PR #34 contract — that change trusted daemon
    persistence, but HERMES_HOME defaults to container-/tmp which is
    volatile across workspace restarts, so a fresh daemon has no
    transcript to replay. Re-attaching client-side history is the
    cheap, surgical fix; daemon-side HERMES_HOME persistence is a
    separate follow-up."""
    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_received: List[Dict[str, Any]] = []

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_received.append(body)
        # Synthesize a reply so execute() returns.
        async def _delayed_reply():
            await asyncio.sleep(0.02)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                await s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "ok",
                        "reply_to": body["message_id"],
                        "metadata": {},
                    },
                )
        asyncio.create_task(_delayed_reply())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
    )
    queue = _CapturingQueue()
    history = [
        {"role": "user", "parts": [{"kind": "text", "text": "old-u"}]},
        {"role": "agent", "parts": [{"kind": "text", "text": "old-a"}]},
    ]
    try:
        await ex.start()
        await ex.execute(
            _build_context_with_history("new-u", history), queue
        )
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(inbound_received) == 1
    body = inbound_received[0]
    assert body["content"] == "new-u"
    # Load-bearing: messages_history must round-trip the canvas history
    # so the plugin-side adapter can seed the daemon transcript.
    assert body.get("messages_history") == history
    # Other payload fields preserved.
    assert set(body.keys()) >= {
        "chat_id", "peer_id", "peer_name", "content",
        "message_id", "callback_url", "messages_history",
    }


@pytest.mark.asyncio
async def test_plugin_path_omits_messages_history_when_no_canvas_history(
    monkeypatch,
):
    """Peer-agent A2A turns (no canvas) ship without metadata.history.
    In that case the executor must omit ``messages_history`` from the
    payload entirely — not send an empty list, which would look like
    "explicitly clear my transcript" to a future plugin contract."""
    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_received: List[Dict[str, Any]] = []

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_received.append(body)
        async def _delayed_reply():
            await asyncio.sleep(0.02)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                await s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "ok",
                        "reply_to": body["message_id"],
                        "metadata": {},
                    },
                )
        asyncio.create_task(_delayed_reply())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        # _build_context sets msg as MagicMock so .metadata is a child
        # mock (truthy non-dict) — _extract_history_from_context's
        # isinstance guard yields [] → omitted.
        await ex.execute(_build_context("hi"), queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(inbound_received) == 1
    assert "messages_history" not in inbound_received[0]


@pytest.mark.asyncio
async def test_plugin_path_skips_messages_history_when_malformed(monkeypatch):
    """Per feedback_surface_actionable_failure_reason_to_user — if
    metadata.history is malformed (not a list), the executor must NOT
    crash the turn. It logs + skips, dispatching the user's message
    stateless. Belt-and-suspenders is best-effort; the user message
    must always reach the daemon."""
    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_received: List[Dict[str, Any]] = []

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_received.append(body)
        async def _delayed_reply():
            await asyncio.sleep(0.02)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                await s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "ok",
                        "reply_to": body["message_id"],
                        "metadata": {},
                    },
                )
        asyncio.create_task(_delayed_reply())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
    )
    queue = _CapturingQueue()
    # Force a malformed history value through the plugin path by
    # monkey-patching the extractor — _extract_history_from_context
    # itself guards against non-list shapes, so we exercise the
    # downstream defense in _execute_via_plugin.
    monkeypatch.setattr(
        HermesAgentProxyExecutor,
        "_extract_history_from_context",
        staticmethod(lambda _ctx: "not-a-list"),
    )
    try:
        await ex.start()
        await ex.execute(_build_context("hi"), queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(inbound_received) == 1
    # Malformed → omitted, turn still dispatched.
    assert "messages_history" not in inbound_received[0]
    assert inbound_received[0]["content"] == "hi"


@pytest.mark.asyncio
async def test_plugin_path_payload_chat_id_uses_context_id_not_task_id(monkeypatch):
    """Wire-level integration test: when the inbound RequestContext
    carries both context_id and task_id, the daemon-side inbound POST
    body's ``chat_id`` must be context_id, NOT task_id.

    Background: hermes daemon's gateway/session.py SessionStore keys
    sessions on session_key derived from SessionSource.chat_id. Per
    a2a-sdk semantics, task_id changes per turn while context_id is the
    stable cross-turn conversation key. If chat_id is keyed on task_id,
    each turn arrives as a fresh session and the SessionStore-owns-state
    contract from RFC #600 doesn't deliver continuity.

    This was caught empirically on 2026-05-20 ~08:00Z in chloe-dong: a
    2-turn name-test ("Hi I'm Hongming" / "what's my name?") produced
    two distinct sqlite session rows (20260520_080028_18798d23 and
    20260520_080035_9abd924a) 7s apart, with the second session's
    assistant explicitly narrating no memory of the first.

    Mirrors workspace-template-openclaw PR #29's
    test_session_id_prefers_context_id_over_task_id, applied to hermes."""
    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_received: List[Dict[str, Any]] = []

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_received.append(body)
        async def _delayed_reply():
            await asyncio.sleep(0.02)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                await s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "ok",
                        "reply_to": body["message_id"],
                        "metadata": {},
                    },
                )
        asyncio.create_task(_delayed_reply())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    # Build a context where both context_id and task_id are populated;
    # the captured payload must use context_id.
    ctx = MagicMock()
    ctx.context_id = "chat-stable-cross-turn"
    ctx.task_id = "task-changes-per-turn"
    ctx.session_id = None
    msg = MagicMock()
    msg.context_id = "chat-stable-cross-turn"
    msg.task_id = "task-changes-per-turn"
    text_part = MagicMock()
    text_part.text = "hello"
    text_part.kind = "text"
    msg.parts = [text_part]
    ctx.message = msg

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(inbound_received) == 1
    body = inbound_received[0]
    # Load-bearing assertion — pins the priority order at the wire layer
    # when WORKSPACE_ID is unset (fallback chain). With WORKSPACE_ID set
    # (the canvas-deployed case, PR #37) the workspace_id-first key wins;
    # see test_plugin_path_payload_chat_id_uses_workspace_id_env.
    assert body["chat_id"] == "chat-stable-cross-turn", (
        f"chat_id must be context_id-derived (stable), got "
        f"{body['chat_id']!r} (likely fell back to task_id-first ordering)"
    )
    # peer_id MUST NOT be aliased to chat_id (PR #37 task #262 fix C):
    # peer_id is the sender's identity, default "" for canvas_user.
    assert body["peer_id"] == "", (
        f"peer_id must default to empty (not chat_id-aliased), "
        f"got {body['peer_id']!r}"
    )


# ---- PR #37 hot-patch: WORKSPACE_ID env-keyed chat_id ----------------


def test_derive_chat_id_prefers_workspace_id_env(monkeypatch):
    """RFC #600 layer-2 hot-patch (task #262). When WORKSPACE_ID is set
    (canvas-deployed case), _derive_chat_id must return a stable
    workspace-keyed identifier — regardless of what context_id /
    task_id carry. This collapses all turns in the same workspace into
    one hermes-daemon SessionStore session.

    Diagnosis context (a60623344): a2a-sdk's
    RequestContext._check_or_generate_context_id mints a fresh UUID per
    turn when the inbound message has no context_id, and the platform
    POST /workspaces/<id>/a2a does not yet thread a canvas-side
    conversation key. Empirically 6 separate sessions appeared in 3
    minutes in /tmp/.hermes/sessions/sessions.json on a single canvas
    chat. Until the platform-side propagation fix lands (separate RFC),
    we pin chat_id to WORKSPACE_ID."""
    monkeypatch.setenv("WORKSPACE_ID", "ws-abc-123")
    ctx = MagicMock()
    # Even with context_id populated, WORKSPACE_ID wins under the
    # hot-patch: context_id is per-turn, workspace is per-conversation.
    ctx.context_id = "fresh-uuid-per-turn"
    ctx.task_id = "task-changes-per-turn"
    ctx.session_id = None
    assert (
        HermesAgentProxyExecutor._derive_chat_id(ctx)
        == "workspace:ws-abc-123"
    )


def test_derive_chat_id_falls_back_to_context_id_when_workspace_id_unset(
    monkeypatch,
):
    """Defensive fallback: when WORKSPACE_ID is unset (local dev,
    non-canvas peer-agent turns), PR #35's context_id-first priority
    chain still applies. Pins backward compatibility for environments
    that don't carry the env."""
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    ctx = MagicMock()
    ctx.context_id = "ctx-still-used"
    ctx.session_id = None
    ctx.task_id = "task-X"
    assert (
        HermesAgentProxyExecutor._derive_chat_id(ctx) == "ctx-still-used"
    )


def test_derive_chat_id_falls_back_when_workspace_id_is_empty_string(
    monkeypatch,
):
    """Defensive: WORKSPACE_ID="" must behave like WORKSPACE_ID unset,
    not produce a degenerate chat_id of "workspace:"."""
    monkeypatch.setenv("WORKSPACE_ID", "")
    ctx = MagicMock()
    ctx.context_id = "ctx-still-used"
    ctx.session_id = None
    ctx.task_id = "task-X"
    assert (
        HermesAgentProxyExecutor._derive_chat_id(ctx) == "ctx-still-used"
    )


# ---- PR #37 hot-patch: peer_id MUST NOT conflate with chat_id --------


@pytest.mark.asyncio
async def test_plugin_path_payload_does_not_conflate_peer_id_with_chat_id(
    monkeypatch,
):
    """Async variant of the decoupling assertion — captures the actual
    wire-level POST body to the hermes daemon and proves:
      * chat_id is the workspace-keyed hot-patch value
      * peer_id is "" (no leak) when no peer metadata is provided
      * peer_name is "" (no leak)
    Per molecule MCP protocol semantics for canvas_user inbound."""
    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_received: List[Dict[str, Any]] = []

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_received.append(body)
        async def _delayed_reply():
            await asyncio.sleep(0.02)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                await s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "ok",
                        "reply_to": body["message_id"],
                        "metadata": {},
                    },
                )
        asyncio.create_task(_delayed_reply())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
        WORKSPACE_ID="ws-canvas-fixture",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(_build_context("hi"), queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(inbound_received) == 1
    body = inbound_received[0]
    assert body["chat_id"] == "workspace:ws-canvas-fixture", (
        f"WORKSPACE_ID hot-patch must win, got chat_id={body['chat_id']!r}"
    )
    assert body["peer_id"] == "", (
        f"peer_id MUST be empty for canvas_user (no chat_id alias), "
        f"got {body['peer_id']!r}"
    )
    assert body["peer_name"] == "", (
        f"peer_name MUST NOT leak chat_id as display string, "
        f"got {body['peer_name']!r}"
    )
    # And critically — peer_id != chat_id even when both happen to be
    # non-empty later. Today the leak symptom is the UUID showing up
    # as the bot's notion of who it's talking to.
    assert body["peer_id"] != body["chat_id"]


@pytest.mark.asyncio
async def test_plugin_path_peer_identity_from_metadata(monkeypatch):
    """When the platform DOES supply peer identity via
    context.message.metadata (peer_agent inbound case), it must be
    propagated to the daemon — not silently dropped or overridden."""
    plugin_port = _free_port()
    cb_port = _free_port()
    inbound_received: List[Dict[str, Any]] = []

    async def fake_inbound(request: web.Request) -> web.Response:
        body = await request.json()
        inbound_received.append(body)
        async def _delayed_reply():
            await asyncio.sleep(0.02)
            async with ClientSession(timeout=ClientTimeout(total=2)) as s:
                await s.post(
                    body["callback_url"],
                    json={
                        "chat_id": body["chat_id"],
                        "content": "ok",
                        "reply_to": body["message_id"],
                        "metadata": {},
                    },
                )
        asyncio.create_task(_delayed_reply())
        return web.json_response({"ok": True, "queued": True})

    plugin_app = web.Application()
    plugin_app.router.add_post("/a2a/inbound", fake_inbound)
    plugin_runner = web.AppRunner(plugin_app)
    await plugin_runner.setup()
    plugin_site = web.TCPSite(plugin_runner, "127.0.0.1", plugin_port)
    await plugin_site.start()

    # Build a peer-agent-style context with metadata.peer_{id,name}.
    ctx = MagicMock()
    ctx.context_id = None
    ctx.session_id = None
    ctx.task_id = "task-peer-1"
    msg = MagicMock()
    msg.task_id = "task-peer-1"
    text_part = MagicMock()
    text_part.text = "hello from peer"
    text_part.kind = "text"
    msg.parts = [text_part]
    msg.metadata = {
        "peer_id": "ws-peer-uuid",
        "peer_name": "ops-agent",
    }
    ctx.message = msg

    ex = _make_executor(
        monkeypatch,
        MOLECULE_A2A_PLATFORM_PORT=str(plugin_port),
        MOLECULE_A2A_CALLBACK_PORT=str(cb_port),
        WORKSPACE_ID="ws-self-canvas",
    )
    queue = _CapturingQueue()
    try:
        await ex.start()
        await ex.execute(ctx, queue)
    finally:
        await ex.stop()
        await plugin_site.stop()
        await plugin_runner.cleanup()

    assert len(inbound_received) == 1
    body = inbound_received[0]
    assert body["chat_id"] == "workspace:ws-self-canvas"
    assert body["peer_id"] == "ws-peer-uuid"
    assert body["peer_name"] == "ops-agent"


def test_derive_peer_identity_handles_missing_metadata():
    """Defensive: missing/None/non-dict metadata → ("", "")."""
    ctx = MagicMock()
    msg = MagicMock()
    msg.metadata = None
    ctx.message = msg
    assert HermesAgentProxyExecutor._derive_peer_identity(ctx) == ("", "")

    ctx2 = MagicMock()
    ctx2.message = None
    assert HermesAgentProxyExecutor._derive_peer_identity(ctx2) == ("", "")

    ctx3 = MagicMock()
    msg3 = MagicMock()
    msg3.metadata = "not-a-dict"
    ctx3.message = msg3
    assert HermesAgentProxyExecutor._derive_peer_identity(ctx3) == ("", "")


def test_derive_peer_identity_handles_partial_metadata():
    """Partial metadata — peer_id without peer_name (or vice versa) —
    yields ("", "") for the missing field."""
    ctx = MagicMock()
    msg = MagicMock()
    # MagicMock returns a child Mock for unknown attrs; we want a real
    # dict so .get() returns None for missing peer_name.
    msg.metadata = {"peer_id": "ws-X"}
    ctx.message = msg
    assert HermesAgentProxyExecutor._derive_peer_identity(ctx) == ("ws-X", "")
