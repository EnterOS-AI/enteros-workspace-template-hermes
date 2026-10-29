"""Turn supervision for the hermes plugin transport.

The executor used to wait a fixed 600 s for a reply and report nothing about
the turn to the runtime. Production consequence (jrs, 2026-10-29): the idle
digest saw a hermes workspace as idle while one turn ran for hours, woke it
every 10 minutes, the gateway queued every wake behind the running turn (cap
32), and the queue then ran for most of a day; each wake's caller got
"[hermes plugin reply timeout after 600s]" and every real answer was dropped
as a late reply.

These tests drive the real executor against a scripted stand-in for the
hermes plugin. scripts/e2e_full_chain.py drives the same behaviours against
the real pinned gateway in CI.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientSession, ClientTimeout, web

import executor as executor_mod
from executor import HermesAgentProxyExecutor
from molecule_runtime.adapters.base import AdapterConfig

WS = "ws-turns"
CHAT = f"workspace:{WS}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


def _ctx(text: str, *, source_type: Optional[str] = None, peer_id: Optional[str] = None):
    """A RequestContext stub. ``source_type`` goes where the runtime's own
    self-wakes put it: the request-level metadata, not the message's."""
    part = MagicMock()
    part.text = text
    part.kind = "text"
    msg = MagicMock()
    msg.task_id = "task"
    msg.message = None  # read the parts off this object (see test_executor_plugin_path)
    msg.parts = [part]
    msg.metadata = {"peer_id": peer_id} if peer_id else None
    ctx = MagicMock()
    ctx.task_id = "task"
    ctx.session_id = None
    ctx.context_id = None
    ctx.message = msg
    ctx.metadata = {"source_type": source_type} if source_type else None
    return ctx


class _Queue:
    def __init__(self) -> None:
        self.events: List[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)

    def texts(self) -> List[str]:
        return [
            "".join(getattr(p, "text", "") or "" for p in getattr(e, "parts", []) or [])
            or repr(e)
            for e in self.events
        ]


class FakeGateway:
    """Stands in for the hermes plugin: records /a2a/inbound and lets a test
    send back to the executor's callback the way the plugin's send() does.
    Answers /stop like hermes does unless told otherwise."""

    def __init__(self, *, answer_stop: bool = True) -> None:
        self.port = _free_port()
        self.inbound: List[Dict[str, Any]] = []
        self.callback_url: Optional[str] = None
        self.answer_stop = answer_stop
        self._runner: Optional[web.AppRunner] = None
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/a2a/inbound", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.inbound.append(body)
        self.callback_url = body["callback_url"]
        if body["content"] == "/stop" and self.answer_stop:
            self._tasks.append(asyncio.create_task(
                self.send("⚡ Stopped.", reply_to=body["message_id"])
            ))
        return web.json_response({"ok": True, "queued": True})

    def contents(self) -> List[str]:
        return [b["content"] for b in self.inbound]

    def message_id(self, content: str) -> str:
        return next(b["message_id"] for b in self.inbound if b["content"] == content)

    async def send(self, content: str, *, reply_to: Optional[str] = None,
                   chat_id: str = CHAT, metadata: Optional[dict] = None):
        body = {"chat_id": chat_id, "content": content, "reply_to": reply_to,
                "metadata": metadata or {}}
        async with ClientSession(timeout=ClientTimeout(total=5)) as s:
            async with s.post(self.callback_url, json=body) as r:
                return r.status, await r.json()


class FakePlatform:
    """The platform endpoints a late reply and a heartbeat push reach."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.notified: List[Dict[str, Any]] = []
        self.fail_next = 0  # answer this many /notify calls with a 500 first
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(f"/workspaces/{WS}/notify", self._notify)
        app.router.add_post("/registry/heartbeat", self._ok)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _notify(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.fail_next > 0:
            self.fail_next -= 1
            return web.json_response({"ok": False}, status=500)
        self.notified.append(body)
        return web.json_response({"ok": True})

    async def _ok(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})


@pytest.fixture(autouse=True)
def _clean_runtime_state(monkeypatch):
    from molecule_runtime import turn_lease
    from molecule_runtime.runtime_inbox import get_inbox

    for name in (
        "PLATFORM_URL", "MOLECULE_WORKSPACE_ID", "MOLECULE_TOOL_ACTIVITY_FILE",
        "HERMES_SESSION_RESET_MODE", "HERMES_REPLY_RECORRELATE",
        "HERMES_DROP_SELF_PINGS_WHEN_BUSY", "MOLECULE_TURN_LEASE_TTL_SECONDS",
        "A2A_COMPLETION_IDLE_TIMEOUT_SECONDS", "MOLECULE_MAX_TURN_SECONDS",
        "HERMES_TURN_STALL_SECONDS", "HERMES_TURN_MAX_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    get_inbox().reset_for_tests()
    previous = turn_lease.current()
    yield
    turn_lease.install(previous)
    get_inbox().reset_for_tests()


def _executor(monkeypatch, gateway: FakeGateway, *, heartbeat=None, **env: str):
    knobs = {
        "MOLECULE_A2A_PLATFORM_ENABLED": "true",
        "MOLECULE_A2A_PLATFORM_PORT": str(gateway.port),
        "MOLECULE_A2A_CALLBACK_PORT": str(_free_port()),
        "WORKSPACE_ID": WS,
        "HERMES_TURN_POLL_SECONDS": "0.02",
        "HERMES_TURN_STALL_SECONDS": "30",
        "HERMES_TURN_MAX_SECONDS": "0",
        "HERMES_CONTROL_REPLY_SECONDS": "1",
    }
    knobs.update(env)
    for name, value in knobs.items():
        monkeypatch.setenv(name, value)
    return HermesAgentProxyExecutor(
        AdapterConfig(model="m", system_prompt="s", heartbeat=heartbeat)
    )


async def _dispatched(gateway: FakeGateway, content: str) -> str:
    assert await _until(lambda: content in gateway.contents()), gateway.contents()
    return gateway.message_id(content)


# --- policy -----------------------------------------------------------------------


def test_policy_defaults_follow_the_runtime_turn_lease(monkeypatch):
    monkeypatch.setenv("MOLECULE_TURN_LEASE_TTL_SECONDS", "120")
    policy = executor_mod.TurnSupervisionPolicy.from_env()
    assert (policy.stall_seconds, policy.max_seconds) == (120.0, 480.0)

    monkeypatch.setenv("MOLECULE_MAX_TURN_SECONDS", "999")
    assert executor_mod.TurnSupervisionPolicy.from_env().max_seconds == 999.0

    monkeypatch.setenv("HERMES_TURN_STALL_SECONDS", "45")
    monkeypatch.setenv("HERMES_TURN_MAX_SECONDS", "0")
    policy = executor_mod.TurnSupervisionPolicy.from_env()
    assert (policy.stall_seconds, policy.max_seconds) == (45.0, 0.0)


def test_bad_knob_values_fall_back_to_defaults(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_TURN_STALL_SECONDS", "soon")
    monkeypatch.setenv("HERMES_TURN_POLL_SECONDS", "0")
    monkeypatch.setenv("HERMES_TURN_MAX_SECONDS", "-5")
    with caplog.at_level("WARNING"):
        policy = executor_mod.TurnSupervisionPolicy.from_env()
    assert policy.stall_seconds == 900.0
    assert policy.poll_seconds == 1.0
    assert policy.max_seconds == 3600.0
    warned = caplog.text
    for name in ("HERMES_TURN_STALL_SECONDS", "HERMES_TURN_POLL_SECONDS", "HERMES_TURN_MAX_SECONDS"):
        assert name in warned


def test_fallback_routine_self_set_matches_the_runtime():
    """The fallback is a copy; this is what keeps it honest."""
    from molecule_runtime.a2a_executor import _ROUTINE_SELF_SOURCE_TYPES

    assert executor_mod._ROUTINE_SELF_SOURCE_TYPES_FALLBACK == frozenset(_ROUTINE_SELF_SOURCE_TYPES)


def test_scheduled_marker_is_read_from_request_level_metadata():
    """The scheduler's trigger lane stamps source_type on the request, as
    every runtime self-send does; the old message-only read never saw it."""
    ctx = _ctx("tick", source_type="self-scheduler")
    assert HermesAgentProxyExecutor._derive_source_type(ctx) == "self-scheduler"
    assert HermesAgentProxyExecutor._derive_source_type(_ctx("tick", source_type="self-idle")) == ""


def test_tool_chip_summary_previews_redacts_and_truncates():
    summary = executor_mod._tool_chip_summary("terminal", json.dumps({"command": "ls -la /tmp"}))
    assert summary == "🛠 terminal: ls -la /tmp"
    leaky = executor_mod._tool_chip_summary(
        "terminal",
        json.dumps({"command": "curl -H 'Authorization: Bearer abcdefghijklmnop0123456789' x"}),
    )
    assert "abcdefghijklmnop0123456789" not in leaky
    long = executor_mod._tool_chip_summary("terminal", json.dumps({"command": "x" * 500}))
    assert long.endswith("…") and len(long) <= len("🛠 terminal: ") + 80
    assert executor_mod._tool_chip_summary("todo", "") == "🛠 todo"
    assert executor_mod._tool_chip_summary("todo", "not json") == "🛠 todo"


# --- a long turn is one turn ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_long_turn_with_gateway_activity_is_not_cut_off(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_TURN_STALL_SECONDS="0.4")
    await ex.start()
    try:
        queue = _Queue()
        running = asyncio.create_task(ex.execute(_ctx("long job"), queue))
        mid = await _dispatched(gateway, "long job")
        # Five stall windows of work, never quiet for one.
        for _ in range(20):
            status, body = await gateway.send("⏳ Working — 1 min")
            assert (status, body) == (200, {"ok": True, "progress": True})
            await asyncio.sleep(0.1)
        await gateway.send("the answer", reply_to=mid)
        await asyncio.wait_for(running, 5)
        assert queue.texts() == ["the answer"]
        assert "/stop" not in gateway.contents()
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_tool_activity_alone_keeps_a_quiet_turn_alive(monkeypatch, tmp_path):
    activity = tmp_path / "tool_activity"
    activity.write_text("")
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(
        monkeypatch, gateway,
        HERMES_TURN_STALL_SECONDS="0.4",
        MOLECULE_TOOL_ACTIVITY_FILE=str(activity),
    )
    await ex.start()
    try:
        queue = _Queue()
        running = asyncio.create_task(ex.execute(_ctx("quiet job"), queue))
        mid = await _dispatched(gateway, "quiet job")
        stamp = time.time()
        for i in range(12):
            os.utime(activity, (stamp + i + 1, stamp + i + 1))
            await asyncio.sleep(0.1)
        assert not running.done(), queue.texts()
        await gateway.send("done quietly", reply_to=mid)
        await asyncio.wait_for(running, 5)
        assert queue.texts() == ["done quietly"]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_silent_gateway_is_abandoned_after_the_stall_window(monkeypatch):
    hb = SimpleNamespace(active_tasks=0, current_task="")
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, heartbeat=hb, HERMES_TURN_STALL_SECONDS="0.3")
    await ex.start()
    try:
        queue = _Queue()
        started = time.monotonic()
        await asyncio.wait_for(ex.execute(_ctx("into the void"), queue), 5)
        assert time.monotonic() - started >= 0.3
        assert len(queue.texts()) == 1
        assert "reply timeout" in queue.texts()[0]
        assert "no activity from the hermes gateway for 0.3s" in queue.texts()[0]
        assert await _until(lambda: "/stop" in gateway.contents())
        assert ex._turns == {}
        assert await _until(lambda: hb.active_tasks == 0)
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_the_turn_cap_stops_the_run_and_answers_everything_queued(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_TURN_MAX_SECONDS="0.5")
    await ex.start()
    try:
        first, second = _Queue(), _Queue()
        running = asyncio.create_task(ex.execute(_ctx("endless"), first))
        await _dispatched(gateway, "endless")
        queued = asyncio.create_task(ex.execute(_ctx("behind it"), second))
        await _dispatched(gateway, "behind it")
        # Busy and alive the whole time: only the cap can end this.
        while not (running.done() and queued.done()):
            await gateway.send("⏳ still going")
            await asyncio.sleep(0.05)
        for queue in (first, second):
            assert len(queue.texts()) == 1
            assert "turn cap" in queue.texts()[0], queue.texts()
        assert await _until(lambda: "/stop" in gateway.contents())
        await asyncio.sleep(0.2)
        assert gateway.contents().count("/stop") == 1
        assert ex._turns == {}
    finally:
        await ex.stop()
        await gateway.close()


# --- busy is reported for as long as the turn runs -------------------------------


@pytest.mark.asyncio
async def test_the_workspace_reports_busy_for_the_whole_turn(monkeypatch):
    from molecule_runtime.runtime_inbox import any_turn_in_flight

    hb = SimpleNamespace(active_tasks=0, current_task="")
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, heartbeat=hb)
    await ex.start()
    try:
        queue = _Queue()
        running = asyncio.create_task(ex.execute(_ctx("work for a while"), queue))
        mid = await _dispatched(gateway, "work for a while")
        assert await _until(lambda: hb.active_tasks == 1 and any_turn_in_flight())
        assert hb.current_task
        await asyncio.sleep(0.3)
        assert hb.active_tasks == 1 and any_turn_in_flight()
        await gateway.send("finished", reply_to=mid)
        await asyncio.wait_for(running, 5)
        assert await _until(lambda: hb.active_tasks == 0 and not any_turn_in_flight())
        assert hb.current_task == ""
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_queued_message_holds_its_own_busy_slot(monkeypatch):
    hb = SimpleNamespace(active_tasks=0, current_task="")
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, heartbeat=hb)
    await ex.start()
    try:
        a, b = _Queue(), _Queue()
        first = asyncio.create_task(ex.execute(_ctx("first"), a))
        first_id = await _dispatched(gateway, "first")
        second = asyncio.create_task(ex.execute(_ctx("second"), b))
        second_id = await _dispatched(gateway, "second")
        assert await _until(lambda: hb.active_tasks == 2)
        await gateway.send("first done", reply_to=first_id)
        await asyncio.wait_for(first, 5)
        assert await _until(lambda: hb.active_tasks == 1)
        await gateway.send("second done", reply_to=second_id)
        await asyncio.wait_for(second, 5)
        assert await _until(lambda: hb.active_tasks == 0)
    finally:
        await ex.stop()
        await gateway.close()


# --- autonomous wakes during a turn ------------------------------------------------


@pytest.mark.asyncio
async def test_a_routine_self_wake_is_dropped_while_a_turn_runs(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        queue = _Queue()
        running = asyncio.create_task(ex.execute(_ctx("real work"), queue))
        mid = await _dispatched(gateway, "real work")
        for source_type in ("self-idle", "self-scheduler", "self-cron", "self-harvester"):
            wake = _Queue()
            await asyncio.wait_for(
                ex.execute(_ctx(f"<SYSTEM IDLE PROMPT> {source_type}", source_type=source_type), wake),
                1,
            )
            assert wake.texts() == [executor_mod._SELF_PING_SKIPPED]
        assert gateway.contents() == ["real work"]

        await gateway.send("work done", reply_to=mid)
        await asyncio.wait_for(running, 5)

        # Idle again: the next wake is a turn like any other.
        later = _Queue()
        woke = asyncio.create_task(
            ex.execute(_ctx("<SYSTEM IDLE PROMPT> later", source_type="self-idle"), later)
        )
        wake_id = await _dispatched(gateway, "<SYSTEM IDLE PROMPT> later")
        await gateway.send("(idle)", reply_to=wake_id)
        await asyncio.wait_for(woke, 5)
        assert later.texts() == ["(idle)"]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_people_peers_and_platform_wakes_still_reach_a_busy_gateway(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        tasks = [asyncio.create_task(ex.execute(_ctx("running"), _Queue()))]
        await _dispatched(gateway, "running")
        tasks.append(asyncio.create_task(ex.execute(_ctx("a person"), _Queue())))
        tasks.append(asyncio.create_task(ex.execute(_ctx("a peer", peer_id="ws-peer"), _Queue())))
        tasks.append(asyncio.create_task(
            ex.execute(_ctx("restart context", source_type="self-restart-context"), _Queue())
        ))
        tasks.append(asyncio.create_task(
            ex.execute(_ctx("liveness check", source_type="self-stall"), _Queue())
        ))
        for content in ("a person", "a peer", "restart context", "liveness check"):
            await _dispatched(gateway, content)
        for content in ("running", "a person", "a peer", "restart context", "liveness check"):
            await gateway.send(f"re: {content}", reply_to=gateway.message_id(content))
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_dropping_self_wakes_can_be_switched_off(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_DROP_SELF_PINGS_WHEN_BUSY="0")
    await ex.start()
    try:
        running = asyncio.create_task(ex.execute(_ctx("busy"), _Queue()))
        await _dispatched(gateway, "busy")
        wake = asyncio.create_task(ex.execute(_ctx("tick", source_type="self-idle"), _Queue()))
        await _dispatched(gateway, "tick")
        for content in ("busy", "tick"):
            await gateway.send("ok", reply_to=gateway.message_id(content))
        await asyncio.wait_for(asyncio.gather(running, wake), 5)
    finally:
        await ex.stop()
        await gateway.close()


# --- replies hermes anchors to the wrong message (hermes#393) ---------------------


@pytest.mark.asyncio
async def test_a_misanchored_final_reaches_the_queued_caller(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        x_queue, y_queue = _Queue(), _Queue()
        x = asyncio.create_task(ex.execute(_ctx("X"), x_queue))
        x_id = await _dispatched(gateway, "X")
        y = asyncio.create_task(ex.execute(_ctx("Y"), y_queue))
        y_id = await _dispatched(gateway, "Y")

        assert await gateway.send("X-DONE", reply_to=x_id) == (200, {"ok": True})
        # The gateway drains Y inside X's run and anchors Y's answer to X.
        status, body = await gateway.send("Y-OK", reply_to=x_id)
        assert (status, body) == (200, {"ok": True, "recorrelated": y_id})
        # A redelivery of that same send is a duplicate, not a second answer.
        assert await gateway.send("Y-OK", reply_to=x_id) == (200, {"ok": True, "stale": True})

        await asyncio.wait_for(asyncio.gather(x, y), 5)
        assert x_queue.texts() == ["X-DONE"]
        assert y_queue.texts() == ["Y-OK"]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_recorrelation_can_be_switched_off(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(
        monkeypatch, gateway, HERMES_REPLY_RECORRELATE="0", HERMES_TURN_STALL_SECONDS="0.4"
    )
    await ex.start()
    try:
        y_queue = _Queue()
        x = asyncio.create_task(ex.execute(_ctx("X"), _Queue()))
        x_id = await _dispatched(gateway, "X")
        y = asyncio.create_task(ex.execute(_ctx("Y"), y_queue))
        await _dispatched(gateway, "Y")
        await gateway.send("X-DONE", reply_to=x_id)
        status, body = await gateway.send("Y-OK", reply_to=x_id)
        assert body.get("recorrelated") is None
        await asyncio.wait_for(asyncio.gather(x, y), 5)
        assert "reply timeout" in y_queue.texts()[0]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_an_older_message_is_released_once_a_newer_one_is_answered(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        x_queue, y_queue = _Queue(), _Queue()
        x = asyncio.create_task(ex.execute(_ctx("lost"), x_queue))
        await _dispatched(gateway, "lost")
        y = asyncio.create_task(ex.execute(_ctx("answered"), y_queue))
        y_id = await _dispatched(gateway, "answered")
        await gateway.send("here you go", reply_to=y_id)
        await asyncio.wait_for(asyncio.gather(x, y), 2)
        assert "moved on to a later message" in x_queue.texts()[0]
        assert y_queue.texts() == ["here you go"]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_send_anchored_to_a_control_message_is_never_recorrelated(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        ex._record_origin("ctl-1", "control")
        ex._note_answered("ctl-1", CHAT)
        y_queue = _Queue()
        y = asyncio.create_task(ex.execute(_ctx("Y"), y_queue))
        y_id = await _dispatched(gateway, "Y")
        status, body = await gateway.send("Session reset, part two", reply_to="ctl-1")
        assert (status, body) == (200, {"ok": True, "stale": True})
        assert not y.done()
        await gateway.send("Y-OK", reply_to=y_id)
        await asyncio.wait_for(y, 5)
        assert y_queue.texts() == ["Y-OK"]
    finally:
        await ex.stop()
        await gateway.close()


# --- anchorless sends ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_anchorless_send_is_progress_and_feeds_the_turn_lease(monkeypatch):
    from molecule_runtime import turn_lease

    lease = turn_lease.TurnLease(ttl_seconds=900)
    turn_lease.install(lease)
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        running = asyncio.create_task(ex.execute(_ctx("tooling"), _Queue()))
        mid = await _dispatched(gateway, "tooling")
        assert not lease.observed_activity
        assert await gateway.send("💬 checking the build") == (200, {"ok": True, "progress": True})
        assert lease.observed_activity
        assert ex._turns[mid].progress == 1
        await gateway.send("done", reply_to=mid)
        await asyncio.wait_for(running, 5)
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_reply_validation_is_unchanged_for_malformed_bodies(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        url = f"http://127.0.0.1:{ex._callback_port}/a2a/reply"
        async with ClientSession(timeout=ClientTimeout(total=2)) as s:
            for payload in ({}, {"reply_to": "x"}, {"reply_to": "x", "content": 5}, [1, 2]):
                async with s.post(url, json=payload) as r:
                    assert r.status == 400, payload
    finally:
        await ex.stop()
        await gateway.close()


# --- late replies ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_late_reply_to_a_self_turn_goes_through_the_idle_forwarder(monkeypatch):
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    monkeypatch.setenv("MOLECULE_WORKSPACE_ID", WS)
    ex = _executor(monkeypatch, gateway, HERMES_TURN_MAX_SECONDS="0.3")
    await ex.start()
    try:
        wake = _Queue()
        running = asyncio.create_task(
            ex.execute(_ctx("<SYSTEM IDLE PROMPT> go", source_type="self-idle"), wake)
        )
        mid = await _dispatched(gateway, "<SYSTEM IDLE PROMPT> go")
        while not running.done():
            await gateway.send("⏳ working")
            await asyncio.sleep(0.05)
        assert "turn cap" in wake.texts()[0]

        status, body = await gateway.send("(idle)", reply_to=mid)
        assert (status, body) == (200, {"ok": True, "stale": True})
        assert platform.notified == []

        status, body = await gateway.send("Fixed the sitemap; PR #9 merged.", reply_to=mid)
        assert (status, body) == (200, {"ok": True, "stale": True, "relayed": True})
        assert platform.notified == [{"message": "Fixed the sitemap; PR #9 merged."}]
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_turn_whose_caller_left_stays_busy_and_its_answer_is_relayed(monkeypatch):
    hb = SimpleNamespace(active_tasks=0, current_task="")
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway, heartbeat=hb)
    await ex.start()
    try:
        running = asyncio.create_task(ex.execute(_ctx("ask the agent"), _Queue()))
        mid = await _dispatched(gateway, "ask the agent")
        assert await _until(lambda: hb.active_tasks == 1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert mid in ex._turns and ex._turns[mid].detached
        await asyncio.sleep(0.1)
        assert hb.active_tasks == 1, "hermes is still running the turn"

        status, body = await gateway.send("the answer, eventually", reply_to=mid)
        assert (status, body) == (200, {"ok": True, "stale": True, "relayed": True})
        assert platform.notified == [{"message": "the answer, eventually"}]
        assert ex._turns == {}
        assert await _until(lambda: hb.active_tasks == 0)
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_failed_late_relay_is_retried_to_the_same_turn(monkeypatch):
    """The redelivery of a late answer whose relay failed carries the same id
    and content; it must not be mistaken for hermes#393 and handed to another
    caller who is still waiting."""
    platform = FakePlatform()
    platform.fail_next = 1
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        left = asyncio.create_task(ex.execute(_ctx("caller leaves"), _Queue()))
        left_id = await _dispatched(gateway, "caller leaves")
        left.cancel()
        with pytest.raises(asyncio.CancelledError):
            await left
        waiting_queue = _Queue()
        waiting = asyncio.create_task(ex.execute(_ctx("caller waits"), waiting_queue))
        waiting_id = await _dispatched(gateway, "caller waits")

        status, _ = await gateway.send("answer for the one who left", reply_to=left_id)
        assert status == 503
        status, body = await gateway.send("answer for the one who left", reply_to=left_id)
        assert (status, body) == (200, {"ok": True, "stale": True, "relayed": True})
        assert platform.notified == [{"message": "answer for the one who left"}]
        assert not waiting.done()

        await gateway.send("answer for the one who waited", reply_to=waiting_id)
        await asyncio.wait_for(waiting, 5)
        assert waiting_queue.texts() == ["answer for the one who waited"]
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


# --- tool events -------------------------------------------------------------------------


def _record(path, *, seq: int, tool: str = "terminal", boot: str = "b1",
            args: str = '{"command": "ls -la"}', status: str = "ok", mtime: float) -> None:
    path.write_text(json.dumps({
        "v": 1, "boot": boot, "seq": seq, "tool": tool, "status": status,
        "args": args, "duration_ms": 3, "ts": mtime,
    }))
    os.utime(path, (mtime, mtime))


@pytest.mark.asyncio
async def test_each_new_tool_event_becomes_one_activity_row(monkeypatch, tmp_path):
    emitted: List[tuple] = []

    async def fake_emit(name, summary=None, status="ok"):
        emitted.append((name, summary, status))

    monkeypatch.setattr(executor_mod, "emit_tool_call", fake_emit)
    activity = tmp_path / "tool_activity"
    stamp = time.time()
    _record(activity, seq=5, mtime=stamp)  # from before this executor started
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, MOLECULE_TOOL_ACTIVITY_FILE=str(activity))
    await ex.start()
    try:
        await asyncio.sleep(0.1)
        assert emitted == [], "an event from before the executor started was replayed"

        _record(activity, seq=6, mtime=stamp + 1)
        assert await _until(lambda: len(emitted) == 1)
        assert emitted[0] == ("terminal", "🛠 terminal: ls -la", "ok")

        os.utime(activity, (stamp + 2, stamp + 2))  # touched, same event
        await asyncio.sleep(0.1)
        assert len(emitted) == 1

        _record(activity, seq=7, tool="read_file", args='{"path": "/x.py"}',
                status="error", mtime=stamp + 3)
        assert await _until(lambda: len(emitted) == 2)
        assert emitted[1] == ("read_file", "🛠 read_file: /x.py", "error")

        _record(activity, seq=1, boot="b2", mtime=stamp + 4)  # the gateway restarted
        assert await _until(lambda: len(emitted) == 3)

        activity.write_text("1")  # the chat-completions path's plain touch
        os.utime(activity, (stamp + 5, stamp + 5))
        await asyncio.sleep(0.1)
        assert len(emitted) == 3
    finally:
        await ex.stop()
        await gateway.close()
