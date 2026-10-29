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


def _ctx(
    text: str, *, source_type: Optional[str] = None, peer_id: Optional[str] = None,
    message_meta: Optional[dict] = None, request_meta: Optional[dict] = None,
):
    """A RequestContext stub. ``source_type`` goes where the runtime's own
    self-wakes put it: the request-level metadata, not the message's."""
    part = MagicMock()
    part.text = text
    part.kind = "text"
    msg = MagicMock()
    msg.task_id = "task"
    msg.message = None  # read the parts off this object (see test_executor_plugin_path)
    msg.parts = [part]
    msg.metadata = dict(message_meta or {}, **({"peer_id": peer_id} if peer_id else {})) or None
    ctx = MagicMock()
    ctx.task_id = "task"
    ctx.session_id = None
    ctx.context_id = None
    ctx.message = msg
    ctx.metadata = dict(request_meta or {}, **({"source_type": source_type} if source_type else {})) or None
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
    Answers /stop like hermes does unless told otherwise; refuses (HTTP 500)
    any content listed in ``refuse``."""

    def __init__(self, *, answer_stop: bool = True) -> None:
        self.port = _free_port()
        self.inbound: List[Dict[str, Any]] = []
        self.callback_url: Optional[str] = None
        self.answer_stop = answer_stop
        self.refuse: set = set()
        self.healthy = True
        self.post_delay = 0.0  # seconds before /a2a/inbound answers
        self._runner: Optional[web.AppRunner] = None
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/a2a/inbound", self._handle)
        app.router.add_get("/a2a/health", self._health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": self.healthy}, status=200 if self.healthy else 503)

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body["content"] in self.refuse:
            return web.json_response({"ok": False}, status=500)
        self.inbound.append(body)
        self.callback_url = body["callback_url"]
        if body["content"] == "/stop" and self.answer_stop:
            self._tasks.append(asyncio.create_task(
                self.send("⚡ Stopped.", reply_to=body["message_id"])
            ))
        if self.post_delay:
            await asyncio.sleep(self.post_delay)
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

    async def answer(self, content: str, reply: str):
        return await self.send(reply, reply_to=self.message_id(content))


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
        "HERMES_SESSION_RESET_MODE", "HERMES_DROP_SELF_PINGS_WHEN_BUSY",
        "MOLECULE_TURN_LEASE_TTL_SECONDS", "A2A_COMPLETION_IDLE_TIMEOUT_SECONDS",
        "MOLECULE_MAX_TURN_SECONDS", "HERMES_TURN_STALL_SECONDS",
        "HERMES_TURN_MAX_SECONDS", "HERMES_TURN_REPLY_DEADLINE_SECONDS",
        "HERMES_SELF_TURN_REPLY_DEADLINE_SECONDS", "HERMES_TURN_QUEUE_MAX",
        "HERMES_AGENT_NOTIFY_INTERVAL", "HERMES_TURN_SETTLE_SECONDS",
        "HERMES_SESSION_IDLE_MINUTES", "HERMES_SESSION_RESET_HOUR",
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


async def _dispatched(gateway: FakeGateway, content: str, timeout: float = 5.0) -> str:
    assert await _until(lambda: content in gateway.contents(), timeout), gateway.contents()
    return gateway.message_id(content)


async def _not_dispatched(gateway: FakeGateway, content: str, wait: float = 0.3) -> None:
    await asyncio.sleep(wait)
    assert content not in gateway.contents(), gateway.contents()


# --- policy -----------------------------------------------------------------------


def test_policy_defaults_follow_the_runtime_turn_lease(monkeypatch):
    monkeypatch.setenv("MOLECULE_TURN_LEASE_TTL_SECONDS", "120")
    policy = executor_mod.TurnSupervisionPolicy.from_env()
    assert (policy.stall_seconds, policy.max_seconds) == (120.0, 480.0)
    # Under the platform's 30-minute A2A ceiling, and under the runtime's
    # shortest self-wake timeout (the delegation harvester's 120 s).
    assert policy.reply_deadline_seconds == 1500.0 < 1800
    assert policy.self_reply_deadline_seconds == 60.0 < 120
    assert policy.queue_max == 32

    monkeypatch.setenv("MOLECULE_MAX_TURN_SECONDS", "999")
    assert executor_mod.TurnSupervisionPolicy.from_env().max_seconds == 999.0

    monkeypatch.setenv("HERMES_TURN_STALL_SECONDS", "45")
    monkeypatch.setenv("HERMES_TURN_MAX_SECONDS", "0")
    monkeypatch.setenv("HERMES_TURN_REPLY_DEADLINE_SECONDS", "0")
    monkeypatch.setenv("HERMES_TURN_QUEUE_MAX", "4")
    policy = executor_mod.TurnSupervisionPolicy.from_env()
    assert (policy.stall_seconds, policy.max_seconds) == (45.0, 0.0)
    assert (policy.reply_deadline_seconds, policy.queue_max) == (0.0, 4)


def test_the_settle_window_follows_the_still_working_notice(monkeypatch):
    # Two notices missed in a row; hermes' own default interval is 180 s.
    assert executor_mod.TurnSupervisionPolicy.from_env().settle_seconds == 370.0
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "60")  # what start.sh sets
    assert executor_mod.TurnSupervisionPolicy.from_env().settle_seconds == 130.0
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0")  # no notice, no settling
    assert executor_mod.TurnSupervisionPolicy.from_env().settle_seconds == 0.0
    monkeypatch.setenv("HERMES_TURN_SETTLE_SECONDS", "45")
    assert executor_mod.TurnSupervisionPolicy.from_env().settle_seconds == 45.0


def test_bad_knob_values_fall_back_to_defaults(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_TURN_STALL_SECONDS", "soon")
    monkeypatch.setenv("HERMES_TURN_POLL_SECONDS", "0")
    monkeypatch.setenv("HERMES_TURN_MAX_SECONDS", "-5")
    monkeypatch.setenv("HERMES_TURN_QUEUE_MAX", "0")
    with caplog.at_level("WARNING"):
        policy = executor_mod.TurnSupervisionPolicy.from_env()
    assert policy.stall_seconds == 900.0
    assert policy.poll_seconds == 1.0
    assert policy.max_seconds == 3600.0
    assert policy.queue_max == 32
    for name in ("HERMES_TURN_STALL_SECONDS", "HERMES_TURN_POLL_SECONDS",
                 "HERMES_TURN_MAX_SECONDS", "HERMES_TURN_QUEUE_MAX"):
        assert name in caplog.text


def test_fallback_routine_self_set_matches_the_runtime():
    """The fallback is a copy; this is what keeps it honest."""
    from molecule_runtime.a2a_executor import _ROUTINE_SELF_SOURCE_TYPES

    assert executor_mod._ROUTINE_SELF_SOURCE_TYPES_FALLBACK == frozenset(_ROUTINE_SELF_SOURCE_TYPES)


def test_delegation_result_wakes_are_never_droppable():
    """The runtime drops them mid-turn because a native turn reads
    DELEGATION_RESULTS_FILE. Hermes does not, so the wake is the only copy."""
    for source_type in ("self-harvester", "self-delegation-result"):
        assert not executor_mod._is_droppable_self_ping(_ctx("results", source_type=source_type), "results")
    untyped = "Delegation results are ready. Review them and take appropriate action:"
    assert not executor_mod._is_droppable_self_ping(_ctx(untyped), untyped)
    for source_type in ("self-idle", "self-scheduler", "self-cron", "self-goal-nudge", "self-lifecycle"):
        assert executor_mod._is_droppable_self_ping(_ctx("tick", source_type=source_type), "tick")
    for source_type in (None, "self-restart-context", "self-stall", "self-nudge"):
        assert not executor_mod._is_droppable_self_ping(_ctx("hello", source_type=source_type), "hello")


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
        assert "/stop" in gateway.contents()
        assert ex._turns == {}
        assert await _until(lambda: hb.active_tasks == 0)
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_the_cap_stops_only_the_running_turn_and_the_queue_moves_on(monkeypatch):
    """hermes' /stop leaves queued plugin messages parked to run next, so only
    the running turn is answered with the cap; the one waiting behind it gets
    its own run, and its own answer."""
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_TURN_MAX_SECONDS="0.5")
    await ex.start()
    try:
        first, second = _Queue(), _Queue()
        running = asyncio.create_task(ex.execute(_ctx("endless"), first))
        await _dispatched(gateway, "endless")
        queued = asyncio.create_task(ex.execute(_ctx("behind it"), second))
        await _not_dispatched(gateway, "behind it", 0.1)
        # Busy and alive the whole time: only the cap can end this.
        while not running.done():
            await gateway.send("⏳ still going")
            await asyncio.sleep(0.05)
        assert len(first.texts()) == 1 and "turn cap" in first.texts()[0], first.texts()
        assert gateway.contents().count("/stop") == 1
        # /stop was answered before the next message went out.
        assert gateway.contents().index("/stop") < gateway.contents().index("behind it") \
            if "behind it" in gateway.contents() else True
        await _dispatched(gateway, "behind it")
        await gateway.answer("behind it", "its own answer")
        await asyncio.wait_for(queued, 5)
        assert second.texts() == ["its own answer"]
        assert gateway.contents().count("/stop") == 1
        assert ex._turns == {}
    finally:
        await ex.stop()
        await gateway.close()


# --- one message at a time per chat ---------------------------------------------


@pytest.mark.asyncio
async def test_the_gateway_never_gets_a_second_message_while_one_runs(monkeypatch):
    """hermes#393: a message hermes queues behind a run is answered under the
    id of the message that opened the run. The executor never lets that
    happen: the next message goes out once the previous one is answered."""
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        queues = [_Queue() for _ in range(3)]
        tasks = []
        for name, queue in zip(("X", "Y", "Z"), queues):
            tasks.append(asyncio.create_task(ex.execute(_ctx(name), queue)))
            await asyncio.sleep(0.02)
        await _dispatched(gateway, "X")
        await _not_dispatched(gateway, "Y")
        await gateway.answer("X", "X-DONE")
        await _dispatched(gateway, "Y")
        await _not_dispatched(gateway, "Z", 0.1)
        await gateway.answer("Y", "Y-OK")
        await _dispatched(gateway, "Z")
        await gateway.answer("Z", "Z-OK")
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert [q.texts() for q in queues] == [["X-DONE"], ["Y-OK"], ["Z-OK"]]
        assert gateway.contents() == ["X", "Y", "Z"]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_second_send_for_an_answered_message_goes_to_its_own_origin(monkeypatch):
    """Nothing is ever re-routed to a waiting caller: a send anchored to an
    already-answered message is that message's late reply."""
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        x_queue, y_queue = _Queue(), _Queue()
        x = asyncio.create_task(ex.execute(_ctx("X"), x_queue))
        x_id = await _dispatched(gateway, "X")
        y = asyncio.create_task(ex.execute(_ctx("Y"), y_queue))
        await gateway.send("X-DONE", reply_to=x_id)
        await asyncio.wait_for(x, 5)
        y_id = await _dispatched(gateway, "Y")
        status, body = await gateway.send("more about X", reply_to=x_id)
        assert (status, body) == (200, {"ok": True, "stale": True, "relayed": True})
        assert platform.notified == [{"message": "more about X"}]
        assert not y.done()
        await gateway.send("Y-OK", reply_to=y_id)
        await asyncio.wait_for(y, 5)
        assert y_queue.texts() == ["Y-OK"]
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_refused_post_frees_the_chat_for_the_next_message(monkeypatch):
    gateway = FakeGateway()
    gateway.refuse.add("refused")
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        first, second, third = _Queue(), _Queue(), _Queue()
        a = asyncio.create_task(ex.execute(_ctx("A"), first))
        await _dispatched(gateway, "A")
        b = asyncio.create_task(ex.execute(_ctx("refused"), second))
        c = asyncio.create_task(ex.execute(_ctx("C"), third))
        await asyncio.sleep(0.05)
        await gateway.answer("A", "A-OK")
        await asyncio.wait_for(b, 5)
        assert "hermes plugin POST error" in second.texts()[0]
        await _dispatched(gateway, "C")
        await gateway.answer("C", "C-OK")
        await asyncio.wait_for(asyncio.gather(a, c), 5)
        assert third.texts() == ["C-OK"]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_full_queue_answers_with_backpressure(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_TURN_QUEUE_MAX="2")
    await ex.start()
    try:
        tasks = [asyncio.create_task(ex.execute(_ctx("running"), _Queue()))]
        await _dispatched(gateway, "running")
        for name in ("w1", "w2"):
            tasks.append(asyncio.create_task(ex.execute(_ctx(name), _Queue())))
        await asyncio.sleep(0.05)
        refused = _Queue()
        await asyncio.wait_for(ex.execute(_ctx("one too many"), refused), 2)
        assert refused.texts() == [executor_mod._QUEUE_FULL_REPLY]
        for name in ("running", "w1", "w2"):
            await _dispatched(gateway, name)
            await gateway.answer(name, "ok")
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert "one too many" not in gateway.contents()
    finally:
        await ex.stop()
        await gateway.close()


# --- busy is reported for as long as a turn runs or waits ---------------------


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
async def test_a_waiting_message_holds_its_own_busy_slot(monkeypatch):
    hb = SimpleNamespace(active_tasks=0, current_task="")
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, heartbeat=hb)
    await ex.start()
    try:
        first = asyncio.create_task(ex.execute(_ctx("first"), _Queue()))
        await _dispatched(gateway, "first")
        second = asyncio.create_task(ex.execute(_ctx("second"), _Queue()))
        assert await _until(lambda: hb.active_tasks == 2)
        await gateway.answer("first", "first done")
        await asyncio.wait_for(first, 5)
        assert await _until(lambda: hb.active_tasks == 1)
        await _dispatched(gateway, "second")
        await gateway.answer("second", "second done")
        await asyncio.wait_for(second, 5)
        assert await _until(lambda: hb.active_tasks == 0)
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_stop_gives_back_busy_slots_and_fails_waiters(monkeypatch):
    hb = SimpleNamespace(active_tasks=0, current_task="")
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, heartbeat=hb)
    await ex.start()
    queue = _Queue()
    running = asyncio.create_task(ex.execute(_ctx("never answered"), queue))
    await _dispatched(gateway, "never answered")
    waiting = asyncio.create_task(ex.execute(_ctx("never sent"), _Queue()))
    assert await _until(lambda: hb.active_tasks == 2)
    try:
        await ex.stop()
        await asyncio.wait_for(asyncio.gather(running, waiting), 5)
        assert "executor shutting down" in queue.texts()[0]
        assert hb.active_tasks == 0
        assert "never sent" not in gateway.contents()
    finally:
        await gateway.close()


# --- autonomous wakes and inline commands during a turn --------------------------


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
        for source_type in ("self-idle", "self-scheduler", "self-cron"):
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
async def test_a_delegation_results_wake_queues_and_is_acked_in_time(monkeypatch):
    """The harvester gives up on its POST after 120 s and resends the results
    if it does; answering it at the self-wake deadline commits them once."""
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_SELF_TURN_REPLY_DEADLINE_SECONDS="0.3")
    await ex.start()
    try:
        running = asyncio.create_task(ex.execute(_ctx("busy"), _Queue()))
        await _dispatched(gateway, "busy")
        ack = _Queue()
        started = time.monotonic()
        await asyncio.wait_for(
            ex.execute(_ctx("Delegation results are ready: ...", source_type="self-harvester"), ack),
            2,
        )
        assert 0.3 <= time.monotonic() - started < 2
        assert ack.texts()[0].startswith("[autonomous turn still running")
        await _not_dispatched(gateway, "Delegation results are ready: ...", 0.1)
        await gateway.answer("busy", "done")
        await asyncio.wait_for(running, 5)
        # It kept its place and runs next.
        await _dispatched(gateway, "Delegation results are ready: ...")
        status, body = await gateway.answer("Delegation results are ready: ...", "Reviewed.")
        assert (status, body) == (200, {"ok": True, "stale": True})  # not shown to anyone
        assert await _until(lambda: ex._turns == {})
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_people_peers_and_platform_wakes_wait_their_turn(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        names = ["running", "a person", "a peer", "restart context", "liveness check"]
        contexts = [
            _ctx("running"),
            _ctx("a person"),
            _ctx("a peer", peer_id="ws-peer"),
            _ctx("restart context", source_type="self-restart-context"),
            _ctx("liveness check", source_type="self-stall"),
        ]
        queues = [_Queue() for _ in names]
        tasks = []
        for ctx, queue in zip(contexts, queues):
            tasks.append(asyncio.create_task(ex.execute(ctx, queue)))
            await asyncio.sleep(0.02)
        for name in names:
            await _dispatched(gateway, name)
            await gateway.answer(name, f"re: {name}")
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert [q.texts() for q in queues] == [[f"re: {n}"] for n in names]
        assert gateway.contents() == names
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
        await _not_dispatched(gateway, "tick", 0.1)
        await gateway.answer("busy", "ok")
        await _dispatched(gateway, "tick")
        await gateway.answer("tick", "ok")
        await asyncio.wait_for(asyncio.gather(running, wake), 5)
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_persons_inline_command_skips_the_queue_and_stop_ends_the_turn(monkeypatch):
    gateway = FakeGateway(answer_stop=False)
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        running_queue, waiting_queue = _Queue(), _Queue()
        running = asyncio.create_task(ex.execute(_ctx("long task"), running_queue))
        await _dispatched(gateway, "long task")
        waiting = asyncio.create_task(ex.execute(_ctx("after it"), waiting_queue))

        status = _Queue()
        checking = asyncio.create_task(ex.execute(_ctx("/status"), status))
        await _dispatched(gateway, "/status")
        await gateway.answer("/status", "Running: long task")
        await asyncio.wait_for(checking, 5)
        assert status.texts() == ["Running: long task"]
        assert not running.done()

        stopping = asyncio.create_task(ex.execute(_ctx("/stop"), _Queue()))
        await _dispatched(gateway, "/stop")
        await gateway.answer("/stop", "⚡ Stopped.")
        await asyncio.wait_for(asyncio.gather(stopping, running), 5)
        assert running_queue.texts() == ["[the turn was interrupted by /stop]"]
        await _dispatched(gateway, "after it")
        await gateway.answer("after it", "next one")
        await asyncio.wait_for(waiting, 5)
        assert waiting_queue.texts() == ["next one"]
    finally:
        await ex.stop()
        await gateway.close()


# --- the reply deadline -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_person_gets_an_interim_reply_at_the_deadline_and_the_answer_later(monkeypatch):
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    hb = SimpleNamespace(active_tasks=0, current_task="")
    ex = _executor(monkeypatch, gateway, heartbeat=hb, HERMES_TURN_REPLY_DEADLINE_SECONDS="0.3")
    await ex.start()
    try:
        queue = _Queue()
        await asyncio.wait_for(ex.execute(_ctx("big job"), queue), 3)
        assert queue.texts()[0].startswith("[hermes is still working on this after 0.3s")
        mid = gateway.message_id("big job")
        assert ex._turns[mid].detached
        assert hb.active_tasks == 1, "the turn is still running"
        status, body = await gateway.send("the big answer", reply_to=mid)
        assert (status, body) == (200, {"ok": True, "stale": True, "relayed": True})
        assert platform.notified == [{"message": "the big answer"}]
        assert await _until(lambda: ex._turns == {} and hb.active_tasks == 0)
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_peer_gets_an_interim_reply_and_the_late_answer_stays_off_the_canvas(monkeypatch):
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway, HERMES_TURN_REPLY_DEADLINE_SECONDS="0.3")
    await ex.start()
    try:
        queue = _Queue()
        await asyncio.wait_for(ex.execute(_ctx("delegated", peer_id="ws-peer"), queue), 3)
        assert "cannot be returned to this request" in queue.texts()[0]
        status, body = await gateway.answer("delegated", "for the peer")
        assert (status, body) == (200, {"ok": True, "stale": True})
        assert platform.notified == []
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_an_idle_wake_is_acked_in_time_and_its_late_answer_forwarded(monkeypatch):
    """The idle digest's poster gives up after 300 s; acked earlier, its
    baseline advances. The answer then goes through the digest's own forwarder,
    which drops "(idle)"."""
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    monkeypatch.setenv("MOLECULE_WORKSPACE_ID", WS)
    ex = _executor(monkeypatch, gateway, HERMES_SELF_TURN_REPLY_DEADLINE_SECONDS="0.3")
    await ex.start()
    try:
        wake = _Queue()
        await asyncio.wait_for(
            ex.execute(_ctx("<SYSTEM IDLE PROMPT> go", source_type="self-idle"), wake), 3
        )
        assert wake.texts()[0].startswith("[autonomous turn still running")
        from molecule_runtime.idle_digest.reply_forwarder import should_suppress

        assert should_suppress(wake.texts()[0]), "the poster must not show the interim reply"

        mid = gateway.message_id("<SYSTEM IDLE PROMPT> go")
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


# --- anchorless sends and late replies -------------------------------------------------


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
async def test_a_long_quiet_turn_reports_still_working_to_the_platform(monkeypatch):
    """No tool calls for minutes: the platform still has to see activity, or
    its stall watchdog restarts the workspace."""
    emitted: List[tuple] = []

    async def fake_emit(name, summary=None, status="ok"):
        emitted.append((name, summary, status))

    monkeypatch.setattr(executor_mod, "emit_tool_call", fake_emit)
    monkeypatch.setattr(executor_mod, "_PROGRESS_ROW_SECONDS", 0.3)
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        running = asyncio.create_task(ex.execute(_ctx("thinking"), _Queue()))
        mid = await _dispatched(gateway, "thinking")
        await gateway.send("⏳ Working — 0 min")
        assert emitted == [], "a young turn reports nothing extra"
        for _ in range(10):
            await asyncio.sleep(0.1)
            await gateway.send("⏳ Working")
        assert 2 <= len(emitted) <= 4, emitted
        assert all(name == "hermes" and summary.startswith("⏳ still working") for name, summary, _ in emitted)
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
        assert await _until(lambda: ex._turns == {} and hb.active_tasks == 0)
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_failed_late_relay_is_retried_and_the_next_message_waits(monkeypatch):
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
        await _not_dispatched(gateway, "caller waits", 0.1)

        status, _ = await gateway.send("answer for the one who left", reply_to=left_id)
        assert status == 503
        status, body = await gateway.send("answer for the one who left", reply_to=left_id)
        assert (status, body) == (200, {"ok": True, "stale": True, "relayed": True})
        assert platform.notified == [{"message": "answer for the one who left"}]

        await _dispatched(gateway, "caller waits")
        await gateway.answer("caller waits", "answer for the one who waited")
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


# --- who is waiting, and until when ------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_runtimes_short_fuse_wakes_get_the_self_deadline(monkeypatch):
    """The platform's first-boot greeting shows the reply text and falls back on
    its own after 90 s; an interim reply there would become the greeting."""
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_SELF_TURN_REPLY_DEADLINE_SECONDS="0.3")
    await ex.start()
    try:
        greet = _Queue()
        greeting = asyncio.create_task(
            ex.execute(_ctx("say hello", source_type="self-first-boot-greet"), greet)
        )
        await _dispatched(gateway, "say hello")
        await asyncio.sleep(0.6)
        assert not greeting.done(), greet.texts()
        await gateway.answer("say hello", "Hi, I'm your agent.")
        await asyncio.wait_for(greeting, 5)
        assert greet.texts() == ["Hi, I'm your agent."]
        assert await _until(lambda: ex._turns == {})

        for source_type in ("self-lifecycle", "self-harvester", "self-idle"):
            wake = _Queue()
            started = time.monotonic()
            await asyncio.wait_for(
                ex.execute(_ctx(f"wake {source_type}", source_type=source_type), wake), 3
            )
            assert 0.3 <= time.monotonic() - started < 2, source_type
            assert wake.texts()[0].startswith("[autonomous turn still running after 0.3s"), wake.texts()
            await gateway.answer(f"wake {source_type}", "ok")
            assert await _until(lambda: ex._turns == {})
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_the_reply_deadline_covers_every_dispatch_of_a_turn(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_TURN_REPLY_DEADLINE_SECONDS="0.6")
    await ex.start()
    try:
        queue = _Queue()
        started = time.monotonic()
        turn = asyncio.create_task(ex.execute(_ctx("summarise everything"), queue))
        await _dispatched(gateway, "summarise everything")
        await asyncio.sleep(0.4)
        # The gateway's ambiguous overflow fallback earns one replay on the same session.
        await gateway.answer(
            "summarise everything", "Sorry, this is too long for me to read all at once."
        )
        assert await _until(lambda: gateway.contents().count("summarise everything") == 2)
        await asyncio.wait_for(turn, 3)
        elapsed = time.monotonic() - started
        assert queue.texts()[0].startswith("[hermes is still working on this after 0.6s"), queue.texts()
        assert elapsed < 0.95, f"the replay got a deadline of its own ({elapsed:.2f}s)"
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_scheduled_reset_waits_for_a_turn_that_starts_alone(monkeypatch):
    """A /new for a message that arrives while another one runs would wait
    behind it, past the new message's own deadline."""
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(
        monkeypatch, gateway,
        HERMES_SESSION_RESET_MODE="daily", HERMES_TURN_REPLY_DEADLINE_SECONDS="0.5",
    )
    await ex.start()
    try:
        # A outlives its caller's deadline and keeps the gateway.
        await asyncio.wait_for(ex.execute(_ctx("A"), _Queue()), 3)
        ex._session_clock._started[CHAT] = time.time() - 2 * 86400  # a boundary has passed
        await asyncio.wait_for(ex.execute(_ctx("B"), _Queue()), 3)
        assert gateway.contents() == ["A"]
        await gateway.answer("A", "A done")
        await _dispatched(gateway, "B")
        await gateway.answer("B", "B done")
        assert await _until(lambda: ex._turns == {})
        assert "/new" not in gateway.contents()

        # The boundary is still there for the next turn that starts alone.
        c = asyncio.create_task(ex.execute(_ctx("C"), _Queue()))
        await _dispatched(gateway, "/new")
        await gateway.answer("/new", "Session reset.")
        await _dispatched(gateway, "C")
        await gateway.answer("C", "C done")
        await asyncio.wait_for(c, 3)
        assert gateway.contents() == ["A", "B", "/new", "C"]
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_persons_stop_ends_only_the_turn_it_was_sent_for(monkeypatch):
    gateway = FakeGateway(answer_stop=False)
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        first, second = _Queue(), _Queue()
        m1 = asyncio.create_task(ex.execute(_ctx("M1"), first))
        await _dispatched(gateway, "M1")
        m2 = asyncio.create_task(ex.execute(_ctx("M2"), second))
        stopping = asyncio.create_task(ex.execute(_ctx("/stop"), _Queue()))
        await _dispatched(gateway, "/stop")
        # M1 finishes before hermes gets to the /stop; M2 goes out.
        await gateway.answer("M1", "M1 done")
        await asyncio.wait_for(m1, 3)
        await _dispatched(gateway, "M2")
        await gateway.answer("/stop", "⚡ Stopped.")
        await asyncio.wait_for(stopping, 3)
        await asyncio.sleep(0.1)
        assert not m2.done(), second.texts()
        await gateway.answer("M2", "M2 done")
        await asyncio.wait_for(m2, 3)
        assert (first.texts(), second.texts()) == (["M1 done"], ["M2 done"])
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_post_that_times_out_counts_as_delivered(monkeypatch):
    """The plugin may already have the message; treating it as refused would
    let the next one land behind it."""
    monkeypatch.setattr(executor_mod, "_INBOUND_POST_TIMEOUT", 0.2)
    gateway = FakeGateway()
    gateway.post_delay = 0.6
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        first, second = _Queue(), _Queue()
        a = asyncio.create_task(ex.execute(_ctx("slow to accept"), first))
        mid = await _dispatched(gateway, "slow to accept")
        b = asyncio.create_task(ex.execute(_ctx("next"), second))
        await asyncio.sleep(0.5)
        assert not a.done(), first.texts()
        assert "next" not in gateway.contents()
        gateway.post_delay = 0.0
        await gateway.send("accepted after all", reply_to=mid)
        await asyncio.wait_for(a, 3)
        assert first.texts() == ["accepted after all"]
        await _dispatched(gateway, "next")
        await gateway.answer("next", "ok")
        await asyncio.wait_for(b, 3)
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_async_delegations_and_channel_turns_are_told_apart(monkeypatch):
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway, HERMES_TURN_REPLY_DEADLINE_SECONDS="0.3")
    await ex.start()
    try:
        # The platform's async delegation; it keeps the reply as the delegator's result.
        delegated = _Queue()
        await asyncio.wait_for(
            ex.execute(_ctx("audit the sitemap", message_meta={"delegation_id": "d-1"}), delegated), 3
        )
        assert "cannot be returned to this request" in delegated.texts()[0]
        assert await gateway.answer("audit the sitemap", "Sitemap is fine.") == (
            200, {"ok": True, "stale": True}
        )
        assert platform.notified == []
        assert await _until(lambda: ex._turns == {})

        # A channel plugin's turn: the runtime stamps the plugin's id as the source.
        channel = _Queue()
        await asyncio.wait_for(
            ex.execute(_ctx("status?", request_meta={"source": "telegram"}), channel), 3
        )
        assert "workspace's own chat" in channel.texts()[0]
        assert await gateway.answer("status?", "All green.") == (
            200, {"ok": True, "stale": True, "relayed": True}
        )
        assert platform.notified == [{"message": "All green."}]
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


# --- finals hermes sends without an anchor ----------------------------------------
# hermes marks every final notify; a run it starts itself (a background job
# finishing) has no anchor, and neither does one of ours that it ran inside such
# a run.


def _final(**extra) -> dict:
    return {"notify": True, **extra}


async def _one_turn(ex, gateway, text="warm up") -> None:
    turn = asyncio.create_task(ex.execute(_ctx(text), _Queue()))
    await _dispatched(gateway, text)
    await gateway.answer(text, "ok")
    await asyncio.wait_for(turn, 3)
    assert await _until(lambda: ex._turns == {})


@pytest.mark.asyncio
async def test_a_final_with_no_anchor_and_nothing_running_reaches_the_chat(monkeypatch):
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        await _one_turn(ex, gateway)
        done = "The build finished: 0 errors."
        assert await gateway.send(done, metadata=_final()) == (200, {"ok": True, "relayed": True})
        assert platform.notified == [{"message": done}]
        # hermes resends a send it got no 2xx for; the same text at once is that resend.
        assert await gateway.send(done, metadata=_final()) == (200, {"ok": True, "relayed": False})
        # A mid-run send is progress whatever else it carries; "(idle)" is housekeeping.
        assert await gateway.send("⏳ Working", metadata=_final(_interim_send=True)) == (
            200, {"ok": True, "progress": True}
        )
        assert await gateway.send("(idle)", metadata=_final()) == (200, {"ok": True, "relayed": False})
        assert platform.notified == [{"message": done}]
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_final_with_no_anchor_is_held_while_a_turn_runs(monkeypatch):
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        queue = _Queue()
        running = asyncio.create_task(ex.execute(_ctx("ask"), queue))
        mid = await _dispatched(gateway, "ask")
        assert await gateway.send("A background job is done.", metadata=_final()) == (
            200, {"ok": True, "held": True}
        )
        assert platform.notified == []
        await gateway.send("the answer", reply_to=mid)
        await asyncio.wait_for(running, 3)
        assert queue.texts() == ["the answer"]
        assert await _until(lambda: platform.notified == [{"message": "A background job is done."}])
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_turn_hermes_ran_inside_its_own_run_gets_its_answer(monkeypatch):
    """Our message landed while hermes ran a turn it started itself: hermes
    answers that run, then ours, both under the run's anchor, which is none."""
    platform = FakePlatform()
    await platform.start()
    gateway = FakeGateway()
    await gateway.start()
    monkeypatch.setenv("PLATFORM_URL", f"http://127.0.0.1:{platform.port}")
    ex = _executor(monkeypatch, gateway, HERMES_TURN_SETTLE_SECONDS="0.3")
    await ex.start()
    try:
        queue = _Queue()
        running = asyncio.create_task(ex.execute(_ctx("what changed?"), queue))
        await _dispatched(gateway, "what changed?")
        await gateway.send("The nightly job finished.", metadata=_final())
        await gateway.send("Two pages changed.", metadata=_final())
        await asyncio.wait_for(running, 3)
        assert queue.texts() == ["Two pages changed."]
        assert await _until(lambda: platform.notified == [{"message": "The nightly job finished."}])
        assert await _until(lambda: ex._turns == {})
    finally:
        await ex.stop()
        await gateway.close()
        await platform.close()


@pytest.mark.asyncio
async def test_a_turn_hermes_ended_silently_is_settled_once_the_gateway_is_idle(monkeypatch):
    """hermes drops a final that is only a silence marker on these internal
    turns; the chat must not wait out the stall window for it."""
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, HERMES_TURN_SETTLE_SECONDS="0.3")
    await ex.start()
    try:
        first, second = _Queue(), _Queue()
        started = time.monotonic()
        silent = asyncio.create_task(ex.execute(_ctx("anything new?"), first))
        await _dispatched(gateway, "anything new?")
        following = asyncio.create_task(ex.execute(_ctx("next"), second))
        await asyncio.wait_for(silent, 3)
        assert first.texts() == [executor_mod._NO_RESPONSE]
        assert 0.3 <= time.monotonic() - started < 2
        await _dispatched(gateway, "next")
        await gateway.answer("next", "ok")
        await asyncio.wait_for(following, 3)
        assert "/stop" not in gateway.contents()
    finally:
        await ex.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_a_quiet_turn_on_a_gateway_that_does_not_answer_waits_for_the_stall(monkeypatch):
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(
        monkeypatch, gateway,
        HERMES_TURN_SETTLE_SECONDS="0.2", HERMES_TURN_STALL_SECONDS="1.0",
    )
    await ex.start()
    try:
        queue = _Queue()
        turn = asyncio.create_task(ex.execute(_ctx("hello?"), queue))
        await _dispatched(gateway, "hello?")
        gateway.healthy = False
        await asyncio.sleep(0.6)
        assert not turn.done(), queue.texts()
        await asyncio.wait_for(turn, 3)
        assert "no activity from the hermes gateway for 1s" in queue.texts()[0]
    finally:
        await ex.stop()
        await gateway.close()
