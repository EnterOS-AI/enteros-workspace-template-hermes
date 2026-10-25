"""Scheduled hermes session reset (issue #383).

hermes stopped rotating gateway conversations on a timer upstream (1d5d0594,
since v0.21.5 and still at v0.22.5): ``session_reset:`` in config.yaml and the
SESSION_IDLE_MINUTES / SESSION_RESET_HOUR variables are ignored, so writing
them from start.sh would be a dead knob. The executor applies the policy
instead, by sending hermes's own ``/new`` ahead of the turn that crosses the
boundary. These tests pin the knob parsing, the boundary arithmetic, the
in-flight guard, and the wire behaviour through ``execute()``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

import executor as ex

_KNOBS = (
    "HERMES_SESSION_RESET_MODE",
    "HERMES_SESSION_RESET_HOUR",
    "HERMES_SESSION_IDLE_MINUTES",
)


def _ts(*args: int) -> float:
    """Epoch of a naive local datetime, the same clock the policy reads."""
    return datetime(*args).timestamp()


@pytest.fixture(autouse=True)
def _clear_knobs(monkeypatch):
    for name in _KNOBS:
        monkeypatch.delenv(name, raising=False)


# --- parsing -----------------------------------------------------------------


def test_unset_knobs_mean_no_resets():
    assert ex.SessionResetPolicy.from_env() == ex.SessionResetPolicy(
        mode="none", at_hour=4, idle_minutes=1440
    )


@pytest.mark.parametrize(
    "raw, mode",
    [("daily", "daily"), ("IDLE", "idle"), (" both ", "both"), ("none", "none")],
)
def test_modes_parse_case_insensitively(monkeypatch, raw, mode):
    monkeypatch.setenv("HERMES_SESSION_RESET_MODE", raw)
    assert ex.SessionResetPolicy.from_env().mode == mode


def test_hour_and_idle_minutes_parse(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_RESET_MODE", "both")
    monkeypatch.setenv("HERMES_SESSION_RESET_HOUR", "15")
    monkeypatch.setenv("HERMES_SESSION_IDLE_MINUTES", "720")
    assert ex.SessionResetPolicy.from_env() == ex.SessionResetPolicy(
        mode="both", at_hour=15, idle_minutes=720
    )


@pytest.mark.parametrize(
    "name, raw, field, default",
    [
        ("HERMES_SESSION_RESET_MODE", "weekly", "mode", "none"),
        ("HERMES_SESSION_RESET_HOUR", "24", "at_hour", 4),
        ("HERMES_SESSION_RESET_HOUR", "-1", "at_hour", 4),
        ("HERMES_SESSION_RESET_HOUR", "15h", "at_hour", 4),
        ("HERMES_SESSION_IDLE_MINUTES", "0", "idle_minutes", 1440),
        ("HERMES_SESSION_IDLE_MINUTES", "ten", "idle_minutes", 1440),
    ],
)
def test_invalid_values_warn_and_fall_back(monkeypatch, caplog, name, raw, field, default):
    """A typo must be loud, and must never stop the workspace from serving."""
    monkeypatch.setenv(name, raw)
    with caplog.at_level(logging.WARNING, logger=ex.logger.name):
        policy = ex.SessionResetPolicy.from_env()
    assert getattr(policy, field) == default
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(name in r.getMessage() for r in warnings), caplog.text


# --- boundary semantics --------------------------------------------------------


def test_daily_resets_the_first_turn_after_the_boundary_once():
    policy = ex.SessionResetPolicy(mode="daily", at_hour=15)
    before = _ts(2026, 10, 11, 14, 50)
    assert policy.reset_reason(_ts(2026, 10, 11, 14, 59), before, before) is None
    assert policy.reset_reason(_ts(2026, 10, 11, 15, 1), before, before) == "daily"
    after = _ts(2026, 10, 11, 15, 1)
    assert policy.reset_reason(_ts(2026, 10, 11, 16, 0), after, after) is None
    # Days of silence still cost exactly one reset, on the next turn.
    assert policy.reset_reason(_ts(2026, 10, 14, 9, 0), before, before) == "daily"


def test_daily_before_the_hour_measures_from_yesterdays_boundary():
    policy = ex.SessionResetPolicy(mode="daily", at_hour=15)
    now = _ts(2026, 10, 12, 10, 0)
    assert policy.reset_reason(now, _ts(2026, 10, 11, 14, 0), now) == "daily"
    assert policy.reset_reason(now, _ts(2026, 10, 11, 16, 0), now) is None


def test_daily_compares_the_previous_turns_start_not_its_end():
    """A tick that started before the boundary and ran across it is stale."""
    policy = ex.SessionResetPolicy(mode="daily", at_hour=15)
    started, finished = _ts(2026, 10, 11, 14, 58), _ts(2026, 10, 11, 15, 4)
    assert policy.reset_reason(_ts(2026, 10, 11, 15, 30), started, finished) == "daily"


def test_idle_measures_from_the_previous_turns_end():
    policy = ex.SessionResetPolicy(mode="idle", idle_minutes=60)
    now = _ts(2026, 10, 11, 12, 0)
    assert policy.reset_reason(now, now - 7200, now - 61 * 60) == "idle"
    assert policy.reset_reason(now, now - 7200, now - 59 * 60) is None


def test_both_applies_either_rule():
    policy = ex.SessionResetPolicy(mode="both", at_hour=15, idle_minutes=60)
    now = _ts(2026, 10, 11, 14, 0)
    assert policy.reset_reason(now, now - 7200, now - 7000) == "idle"
    now = _ts(2026, 10, 11, 15, 10)
    assert policy.reset_reason(now, now - 20 * 60, now - 15 * 60) == "daily"


@pytest.mark.parametrize("mode", ["idle", "daily", "both"])
def test_the_first_turn_on_a_chat_never_resets(mode):
    """A container restart is not a boundary (hermes's own rule)."""
    policy = ex.SessionResetPolicy(mode=mode, at_hour=0, idle_minutes=1)
    assert policy.reset_reason(_ts(2026, 10, 11, 12, 0), None, None) is None


def test_none_never_resets():
    policy = ex.SessionResetPolicy()
    assert policy.reset_reason(_ts(2030, 1, 1, 0, 0), 0.0, 0.0) is None


# --- per-chat clock --------------------------------------------------------------


def test_clock_never_resets_a_chat_with_a_turn_in_flight():
    now = [_ts(2026, 10, 11, 14, 59)]
    clock = ex._SessionResetClock(
        ex.SessionResetPolicy(mode="daily", at_hour=15), clock=lambda: now[0]
    )
    assert clock.begin("chat") is None  # first turn, 14:59
    now[0] = _ts(2026, 10, 11, 15, 1)
    assert clock.begin("chat") is None  # overlaps the 14:59 turn
    clock.end("chat")
    clock.end("chat")
    # The overlapping turn did not move the start clock past the boundary,
    # so the next turn that starts alone still gets the day's reset.
    now[0] = _ts(2026, 10, 11, 15, 30)
    assert clock.begin("chat") == "daily"
    clock.end("chat")
    now[0] = _ts(2026, 10, 11, 16, 0)
    assert clock.begin("chat") is None


def test_clock_keeps_chats_apart():
    now = [_ts(2026, 10, 11, 12, 0)]
    clock = ex._SessionResetClock(
        ex.SessionResetPolicy(mode="idle", idle_minutes=1), clock=lambda: now[0]
    )
    clock.begin("a")
    clock.end("a")
    now[0] += 120
    assert clock.begin("b") is None
    assert clock.begin("a") == "idle"


# --- through execute() -------------------------------------------------------------


class _Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


def _executor(monkeypatch, *, release=None, block_on=1, **knobs):
    """A real executor whose transport records every dispatched content.

    With ``release`` set, the ``block_on``-th dispatch waits for it.
    """
    for name, value in knobs.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("WORKSPACE_ID", "ws-reset")
    monkeypatch.setattr(ex, "extract_message_text", lambda message: "tick")
    monkeypatch.setattr(ex, "extract_attached_files", lambda message: [])
    monkeypatch.setattr(ex, "_session_jsonl_snapshot", lambda: {})
    monkeypatch.setattr(ex, "_tool_trace_from_session_delta", lambda snap: [])
    from molecule_runtime.adapters.base import AdapterConfig

    inst = ex.HermesAgentProxyExecutor(AdapterConfig(model="m", system_prompt="s"))
    sent = []

    async def fake_dispatch(self, *, content, chat_id, **_kwargs):
        sent.append((content, chat_id))
        if release is not None and len(sent) == block_on:
            await release.wait()
        return "Session reset." if content == "/new" else "done"

    monkeypatch.setattr(ex.HermesAgentProxyExecutor, "_dispatch_and_wait", fake_dispatch)
    return inst, sent


def _ctx():
    return SimpleNamespace(message=None)


def test_unset_knobs_never_send_new(monkeypatch):
    inst, sent = _executor(monkeypatch)
    assert inst._session_clock is None
    asyncio.run(inst.execute(_ctx(), _Queue()))
    asyncio.run(inst.execute(_ctx(), _Queue()))
    assert [content for content, _ in sent] == ["tick", "tick"]


def test_daily_boundary_sends_new_before_the_turn(monkeypatch):
    inst, sent = _executor(
        monkeypatch, HERMES_SESSION_RESET_MODE="daily", HERMES_SESSION_RESET_HOUR="15"
    )
    now = [_ts(2026, 10, 11, 14, 50)]
    inst._session_clock = ex._SessionResetClock(inst._session_clock.policy, clock=lambda: now[0])

    queue = _Queue()
    asyncio.run(inst.execute(_ctx(), queue))
    now[0] = _ts(2026, 10, 11, 15, 5)
    asyncio.run(inst.execute(_ctx(), queue))
    now[0] = _ts(2026, 10, 11, 15, 35)
    asyncio.run(inst.execute(_ctx(), queue))

    assert [content for content, _ in sent] == ["tick", "/new", "tick", "tick"]
    assert {chat for _, chat in sent} == {"workspace:ws-reset"}, (
        "the /new must target the same hermes chat as the turn it precedes"
    )
    assert len(queue.events) == 3, "the reset's own reply must not reach the caller"


def test_no_new_while_the_chat_has_a_turn_in_flight(monkeypatch):
    """/new on a busy chat would interrupt the running turn; wait instead."""

    async def scenario():
        release = asyncio.Event()
        inst, sent = _executor(
            monkeypatch,
            release=release,
            block_on=2,
            HERMES_SESSION_RESET_MODE="idle",
            HERMES_SESSION_IDLE_MINUTES="1",
        )
        now = [_ts(2026, 10, 11, 12, 0)]
        inst._session_clock = ex._SessionResetClock(
            inst._session_clock.policy, clock=lambda: now[0]
        )
        await inst.execute(_ctx(), _Queue())  # completes at 12:00
        now[0] += 30
        running = asyncio.create_task(inst.execute(_ctx(), _Queue()))
        await asyncio.sleep(0)  # now parked inside its dispatch
        now[0] += 3600  # idle by the clock, but a turn is in flight
        await inst.execute(_ctx(), _Queue())
        release.set()
        await running
        return [content for content, _ in sent]

    assert asyncio.run(scenario()) == ["tick", "tick", "tick"]
