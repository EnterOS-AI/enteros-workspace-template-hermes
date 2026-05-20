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
from molecule_runtime.adapters.base import AdapterConfig


# ---- helpers --------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_executor(monkeypatch, **env: str) -> HermesAgentProxyExecutor:
    """Test helper. Defaults MOLECULE_A2A_PLATFORM_ENABLED=true so the
    plugin-path tests below operate in plugin mode by default. Tests
    that exercise the chat_completions fallback override this."""
    env.setdefault("MOLECULE_A2A_PLATFORM_ENABLED", "true")
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
async def test_execute_empty_prompt_short_circuits(monkeypatch):
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
    assert "empty prompt" in repr(queue.events[0])


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


# ---- chat_id derivation ----------------------------------------------


def test_derive_chat_id_prefers_task_id():
    ctx = MagicMock()
    ctx.task_id = "task-A"
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "task-A"


def test_derive_chat_id_falls_back_to_session_id():
    ctx = MagicMock()
    ctx.task_id = None
    ctx.session_id = "sess-B"
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "sess-B"


def test_derive_chat_id_synthesizes_when_nothing_present():
    ctx = MagicMock()
    ctx.task_id = None
    ctx.session_id = None
    ctx.context_id = None
    ctx.message = None
    derived = HermesAgentProxyExecutor._derive_chat_id(ctx)
    assert derived.startswith("adhoc-")


def test_derive_chat_id_falls_back_to_message_attrs():
    """When ctx.task_id/session_id/context_id are all None but
    ctx.message has one of them, derive_chat_id should use that."""
    ctx = MagicMock()
    ctx.task_id = None
    ctx.session_id = None
    ctx.context_id = None
    msg = MagicMock()
    msg.task_id = None
    msg.session_id = None
    msg.context_id = "ctx-from-message"
    ctx.message = msg
    assert HermesAgentProxyExecutor._derive_chat_id(ctx) == "ctx-from-message"


def test_derive_chat_id_uses_message_id_camelcase():
    """The a2a-sdk uses messageId on the Message object — ensure we
    pick it up as a last fallback before synthesizing an adhoc id."""
    ctx = MagicMock()
    ctx.task_id = None
    ctx.session_id = None
    ctx.context_id = None
    msg = MagicMock()
    msg.task_id = None
    msg.session_id = None
    msg.context_id = None
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
async def test_plugin_path_forwards_history_to_daemon(monkeypatch):
    """When MOLECULE_A2A_PLATFORM_ENABLED=true, the executor must also
    include messages_history in the POST to /a2a/inbound so the hermes
    daemon can replay it on its side."""
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
    assert body["messages_history"] == history
