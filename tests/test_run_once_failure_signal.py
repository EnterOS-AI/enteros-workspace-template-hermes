"""In a Light run, a turn the executor gave up on is a FAILED task (contract C6).

A Light run pod (MOLECULE_RUN_MODE=once) sends one scheduled prompt through
execute() and records the run from what the executor enqueued: an answer is a
succeeded run; a FAILED task status is a failed one. The executor composes a
reply of its own whenever a turn did not run to hermes' answer (a stall, the
turn cap, a refused POST, the reply-deadline interim, a cancellation, ...).
Outside a run that stays a plain reply, byte for byte; in a run it must be a
FAILED status carrying the same text, or the run is recorded as succeeded.

The scenarios drive the real executor against the scripted plugin of
tests/test_turn_supervision.py, and the outcome is read with the runtime's own
classifier and, end to end, its own once-mode runner.
"""
from __future__ import annotations

import ast
import asyncio
import collections
import contextlib
import dataclasses
import json
import pathlib
import time
from types import SimpleNamespace

import pytest
import yaml
from a2a.types import Message, TaskState, TaskStatusUpdateEvent
from aiohttp import web

import executor as executor_mod
from executor import HermesAgentProxyExecutor
from molecule_runtime.adapters.base import AdapterConfig
from molecule_runtime.run_once import RunSpec, build_run_context, classify_turn, run_once
from tests.test_turn_supervision import (  # noqa: F401 - the autouse fixture is used by name
    FakeGateway,
    _clean_runtime_state,
    _ctx,
    _dispatched,
    _executor,
    _free_port,
    _Queue,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]

SPEC = RunSpec(
    run_id="light-ws-abc-123",
    schedule="daily-report",
    prompt="write the weekly report",
    deadline_seconds=3600,
    workspace_id="11111111-2222-3333-4444-555555555555",
    platform_url="http://platform.test",
    started_at=0.0,
    started_monotonic=0.0,
)


@pytest.fixture(autouse=True)
def _serve_mode_unless_told(monkeypatch):
    monkeypatch.delenv("MOLECULE_RUN_MODE", raising=False)


def _run_ctx(prompt: str = SPEC.prompt):
    """The turn a Light run sends: the runtime's own once-mode context."""
    return build_run_context(dataclasses.replace(SPEC, prompt=prompt))


def _text(message) -> str:
    return "".join(getattr(part, "text", "") or "" for part in message.parts)


@contextlib.asynccontextmanager
async def _running(monkeypatch, *, gateway=None, heartbeat=None, **env):
    gateway = gateway or FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway, heartbeat=heartbeat, **env)
    await ex.start()
    try:
        yield ex, gateway
    finally:
        await ex.stop()
        await gateway.close()


@contextlib.asynccontextmanager
async def _chat_completions(monkeypatch, handler):
    """An executor on the chat-completions fallback, against ``handler``
    (None = nothing listening)."""
    port = _free_port()
    runner = None
    if handler is not None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    monkeypatch.setenv("MOLECULE_A2A_PLATFORM_ENABLED", "false")
    monkeypatch.setenv("HERMES_API_BASE", f"http://127.0.0.1:{port}/v1")

    async def no_peers(self):
        return ""

    monkeypatch.setattr(HermesAgentProxyExecutor, "_fetch_peers_blurb", no_peers)
    ex = HermesAgentProxyExecutor(AdapterConfig(model="m", system_prompt="s"))
    try:
        yield ex
    finally:
        await ex.stop()
        if runner is not None:
            await runner.cleanup()


# --- the give-up paths (P1-P9 on the plugin transport, C1-C3 on the fallback) ----


async def _empty_prompt(monkeypatch):  # P1
    async with _running(monkeypatch) as (ex, _gateway):
        queue = _Queue()
        await ex.execute(_run_ctx("   "), queue)
    return queue, "Your message was empty. Please send text or a file with instructions."


async def _repeat_wake_dropped(monkeypatch):  # P2
    async with _running(monkeypatch) as (ex, gateway):
        first = asyncio.create_task(ex.execute(_run_ctx(), _Queue()))
        await _dispatched(gateway, SPEC.prompt)
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
        await gateway.answer(SPEC.prompt, "done")
        await first
    return queue, executor_mod._SELF_PING_SKIPPED


async def _queue_full(monkeypatch):  # P3
    async with _running(monkeypatch, HERMES_TURN_QUEUE_MAX="1") as (ex, gateway):
        running = asyncio.create_task(ex.execute(_ctx("running"), _Queue()))
        await _dispatched(gateway, "running")
        waiting = asyncio.create_task(ex.execute(_ctx("waiting"), _Queue()))
        await asyncio.sleep(0.05)
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
        for name in ("running", "waiting"):
            await _dispatched(gateway, name)
            await gateway.answer(name, "ok")
        await asyncio.gather(running, waiting)
    return queue, executor_mod._QUEUE_FULL_REPLY


async def _shutting_down(monkeypatch):  # P4
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    await ex.stop()
    try:
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    finally:
        await gateway.close()
    return queue, "[hermes plugin error] executor shutting down"


async def _reply_deadline(monkeypatch):  # P5
    async with _running(monkeypatch, HERMES_TURN_REPLY_DEADLINE_SECONDS="0.3") as (ex, gateway):
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
        await gateway.answer(SPEC.prompt, "late answer")
    return queue, "[autonomous turn still running after 0.3s"


async def _cancelled(monkeypatch):  # P6
    async with _running(monkeypatch) as (ex, gateway):
        queue = _Queue()
        turn = asyncio.create_task(ex.execute(_run_ctx(), queue))
        mid = await _dispatched(gateway, SPEC.prompt)
        ex._pending[mid].cancel()
        await turn
    return queue, "[hermes plugin error] the turn was cancelled"


async def _stall(monkeypatch):  # P7
    async with _running(monkeypatch, HERMES_TURN_STALL_SECONDS="0.3") as (ex, _gateway):
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    return queue, "no activity from the hermes gateway for 0.3s; the turn was abandoned"


async def _cap(monkeypatch):  # P7
    async with _running(monkeypatch, HERMES_TURN_MAX_SECONDS="0.5") as (ex, gateway):
        queue = _Queue()
        turn = asyncio.create_task(ex.execute(_run_ctx(), queue))
        await _dispatched(gateway, SPEC.prompt)
        while not turn.done():  # busy and alive the whole time: only the cap ends it
            await gateway.send("⏳ still going")
            await asyncio.sleep(0.05)
    return queue, "the turn ran past the 0.5s turn cap and was stopped"


async def _post_refused(monkeypatch):  # P7
    gateway = FakeGateway()
    gateway.refuse.add(SPEC.prompt)
    async with _running(monkeypatch, gateway=gateway) as (ex, _gateway):
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    return queue, "[hermes plugin POST error]"


async def _interrupted_by_stop(monkeypatch):  # P7
    async with _running(monkeypatch, gateway=FakeGateway(answer_stop=False)) as (ex, gateway):
        queue = _Queue()
        running = asyncio.create_task(ex.execute(_run_ctx(), queue))
        await _dispatched(gateway, SPEC.prompt)
        stopping = asyncio.create_task(ex.execute(_ctx("/stop"), _Queue()))
        await _dispatched(gateway, "/stop")
        await gateway.answer("/stop", "⚡ Stopped.")
        await asyncio.gather(stopping, running)
    return queue, "[the turn was interrupted by /stop]"


async def _stopped_mid_turn(monkeypatch):  # P8
    gateway = FakeGateway()
    await gateway.start()
    ex = _executor(monkeypatch, gateway)
    await ex.start()
    try:
        queue = _Queue()
        turn = asyncio.create_task(ex.execute(_run_ctx(), queue))
        await _dispatched(gateway, SPEC.prompt)
        await ex.stop()
        await turn
    finally:
        await gateway.close()
    return queue, "[hermes plugin error] executor shutting down"


_OVERFLOW = "Context length exceeded (182,430 tokens). Cannot compress further."


async def _overflow_not_healed(monkeypatch):  # P9: the replay on a fresh session overflows again
    replies = [_OVERFLOW, "Session reset! Starting fresh.", _OVERFLOW]

    async def scripted(self, *, content, chat_id, peer_id, peer_name, callback_url, history,
                       event_queue, source_type=""):
        return replies.pop(0)

    monkeypatch.setattr(HermesAgentProxyExecutor, "_dispatch_and_wait", scripted)
    ex = _executor(monkeypatch, FakeGateway())
    queue = _Queue()
    await ex.execute(_run_ctx(), queue)
    assert replies == []
    return queue, _OVERFLOW


async def _cc_http_error(monkeypatch):  # C1
    async def error(_request):
        return web.Response(status=500, text="boom")

    async with _chat_completions(monkeypatch, error) as ex:
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    return queue, "[hermes-agent error 500] boom"


async def _cc_unreachable(monkeypatch):  # C2
    async with _chat_completions(monkeypatch, None) as ex:
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    return queue, "[hermes-agent unreachable]"


async def _cc_tool_rounds(monkeypatch):  # C3
    async def always_a_tool_call(_request):
        return web.json_response({"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "list_peers", "arguments": "{}"}}],
        }}]})

    async def tool_result(self, tool_call):
        return "ok"

    monkeypatch.setattr(HermesAgentProxyExecutor, "_dispatch_tool", tool_result)
    async with _chat_completions(monkeypatch, always_a_tool_call) as ex:
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    return queue, "(hermes-agent returned no content)"


GIVE_UPS = {
    "empty-prompt": _empty_prompt,
    "repeat-wake-dropped": _repeat_wake_dropped,
    "queue-full": _queue_full,
    "shutting-down": _shutting_down,
    "reply-deadline": _reply_deadline,
    "cancelled": _cancelled,
    "stall": _stall,
    "cap": _cap,
    "post-refused": _post_refused,
    "interrupted-by-stop": _interrupted_by_stop,
    "stopped-mid-turn": _stopped_mid_turn,
    "overflow-not-healed": _overflow_not_healed,
    "cc-http-error": _cc_http_error,
    "cc-unreachable": _cc_unreachable,
    "cc-tool-rounds": _cc_tool_rounds,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["serve", "once"])
@pytest.mark.parametrize("scenario", list(GIVE_UPS))
async def test_a_turn_the_executor_gave_up_on(monkeypatch, scenario, mode):
    if mode == "once":
        monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    queue, reason = await asyncio.wait_for(GIVE_UPS[scenario](monkeypatch), 20)
    assert len(queue.events) == 1, queue.events
    (event,) = queue.events
    if mode == "serve":  # today's reply, unchanged
        assert isinstance(event, Message), event
        assert reason in _text(event)
        return
    assert isinstance(event, TaskStatusUpdateEvent), event
    assert event.status.state == TaskState.TASK_STATE_FAILED
    assert reason in _text(event.status.message)
    result = classify_turn(queue.events)  # the runtime's own reading of the turn
    assert (result.status, result.output, result.exit_code) == ("failed", "", 1)
    assert reason in result.error


# --- answers stay answers --------------------------------------------------------


async def _answer(monkeypatch):
    async with _running(monkeypatch) as (ex, gateway):
        queue = _Queue()
        turn = asyncio.create_task(ex.execute(_run_ctx(), queue))
        await _dispatched(gateway, SPEC.prompt)
        await gateway.answer(SPEC.prompt, "the weekly report")
        await turn
    return queue, "the weekly report"


async def _silent_end(monkeypatch):
    # hermes ended the run with nothing under the turn's id (a bare [SILENT]): a
    # scheduled task may finish quietly on purpose, so this is no failure.
    async with _running(monkeypatch, HERMES_TURN_SETTLE_SECONDS="0.3") as (ex, _gateway):
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    return queue, executor_mod._NO_RESPONSE


async def _cc_answer(monkeypatch):
    async def answer(_request):
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "the weekly report"}}]})

    async with _chat_completions(monkeypatch, answer) as ex:
        queue = _Queue()
        await ex.execute(_run_ctx(), queue)
    return queue, "the weekly report"


ANSWERS = {"answer": _answer, "silent-end": _silent_end, "cc-answer": _cc_answer}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(ANSWERS))
async def test_an_answer_stays_an_answer_in_once_mode(monkeypatch, scenario):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    queue, text = await asyncio.wait_for(ANSWERS[scenario](monkeypatch), 20)
    (event,) = queue.events
    assert isinstance(event, Message) and _text(event) == text
    result = classify_turn(queue.events)
    assert (result.status, result.output, result.exit_code) == ("succeeded", text, 0)


# --- a model call the provider refused --------------------------------------------
#
# hermes answers such a turn with its own explanation as the reply text (the
# Phase 1 gate's run: 402 insufficient_balance, recorded as succeeded). The
# image's molecule_turn_activity plugin records each model call's outcome as
# hermes classified it; these tests write that record the way the plugin does,
# at the moment the gateway's model call ends: after the dispatch, before hermes
# answers.

# hermes' reply for the gate's 402 (agent/conversation_loop.py _billing_failure_result).
_BILLING_TEXT = (
    "Billing or credits exhausted: Error code: 402 - insufficient_balance\n\n"
    "Custom reported that billing, credits, or account entitlement is exhausted for minimax/MiniMax-M2.7.\n"
    "Add credits or update billing with that provider, then retry."
)


def _model_call(path: pathlib.Path, *, ok: bool, status=None, reason="", at=None) -> None:
    path.write_text(json.dumps({
        "v": 1, "ok": ok, "status_code": status, "reason": reason, "retryable": False,
        "provider": "custom", "model": "minimax/MiniMax-M2.7", "ts": time.time() if at is None else at,
    }), encoding="utf-8")


async def _hermes_answers_after(monkeypatch, tmp_path, records, text=_BILLING_TEXT, *, before_turn=()):
    """One turn on the plugin transport: ``records`` are written once the turn
    is dispatched, ``before_turn`` before it starts; then hermes answers ``text``."""
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv("MOLECULE_MODEL_CALL_FILE", str(model_calls))
    for record in before_turn:
        _model_call(model_calls, **record)
    async with _running(monkeypatch) as (ex, gateway):
        queue = _Queue()
        turn = asyncio.create_task(ex.execute(_run_ctx(), queue))
        await _dispatched(gateway, SPEC.prompt)
        for record in records:
            _model_call(model_calls, **record)
        await gateway.answer(SPEC.prompt, text)
        await turn
    return queue


REFUSALS = {
    # The gate's case: credits exhausted.
    "billing-402": ({"status": 402, "reason": "billing"}, "billing, HTTP 402"),
    # Quota or rate limit, retries exhausted.
    "rate-limit-429": ({"status": 429, "reason": "rate_limit"}, "rate_limit, HTTP 429"),
    "upstream-rate-limit-429": ({"status": 429, "reason": "upstream_rate_limit"}, "upstream_rate_limit, HTTP 429"),
    # A key the provider rejects.
    "auth-401": ({"status": 401, "reason": "auth_permanent"}, "auth_permanent, HTTP 401"),
    "auth-403": ({"status": 403, "reason": "auth"}, "auth, HTTP 403"),
    # This account may not use the model (no status: hermes read it from the body).
    "entitlement": ({"reason": "model_entitlement"}, "model_entitlement"),
    # A refusing status hermes could not classify (a WAF 403 in front of the provider).
    "status-only-403": ({"status": 403, "reason": "upstream_blocked"}, "upstream_blocked, HTTP 403"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["serve", "once"])
@pytest.mark.parametrize("case", list(REFUSALS))
async def test_a_turn_the_provider_refused(monkeypatch, tmp_path, case, mode):
    if mode == "once":
        monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    record, reason = REFUSALS[case]
    queue = await asyncio.wait_for(
        _hermes_answers_after(monkeypatch, tmp_path, [{"ok": False, **record}]), 20
    )
    (event,) = queue.events
    if mode == "serve":  # hermes' reply, byte for byte, as before
        assert isinstance(event, Message) and _text(event) == _BILLING_TEXT
        return
    assert isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_FAILED
    assert _text(event.status.message) == (
        f"the model provider refused the call ({reason}; custom minimax/MiniMax-M2.7): {_BILLING_TEXT}"
    )
    result = classify_turn(queue.events)
    assert (result.status, result.output, result.exit_code) == ("failed", "", 1)
    assert result.error.startswith(f"the model provider refused the call ({reason}")


# --- a last model call that failed some other way ---------------------------------
#
# hermes fires api_request_error for EVERY failed attempt of a main-loop model
# call and post_api_request for every one that answers (agent/turn_api_error.py,
# turn_response_check.py, turn_truncation.py and turn_response_intake.py at
# HERMES_COMMIT), so the record is the turn's last attempt. When that attempt
# failed, nothing answered after it: the text hermes replies with is its own
# account of the failure, whatever the reason. These used to be recorded as
# succeeded runs, with that account as the run's output.

_SERVER_ERROR_TEXT = "API call failed after 3 retries: HTTP 500: upstream connect error"

FAILED_CALLS = {
    # The provider, or the proxy in front of it, is failing or overloaded.
    "server-error-500": ({"status": 500, "reason": "server_error"}, "server_error, HTTP 500"),
    "bad-gateway-502": ({"status": 502, "reason": "server_error"}, "server_error, HTTP 502"),
    "overloaded-503": ({"status": 503, "reason": "overloaded"}, "overloaded, HTTP 503"),
    # No status: the call never got a response.
    "timeout": ({"reason": "timeout"}, "timeout"),
    # Empty or malformed responses, retries exhausted.
    "invalid-response": ({"reason": "invalid_response"}, "invalid_response"),
    # The model declined the prompt and no fallback answered.
    "content-policy": ({"reason": "content_policy_blocked"}, "content_policy_blocked"),
    # A request the provider rejects outright.
    "bad-request-400": ({"status": 400, "reason": "format_error"}, "format_error, HTTP 400"),
    "model-not-found-404": ({"status": 404, "reason": "model_not_found"}, "model_not_found, HTTP 404"),
    # hermes could not classify it.
    "unknown": ({"reason": "unknown"}, "unknown"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["serve", "once"])
@pytest.mark.parametrize("case", list(FAILED_CALLS))
async def test_a_turn_whose_last_model_call_failed(monkeypatch, tmp_path, case, mode):
    if mode == "once":
        monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    record, reason = FAILED_CALLS[case]
    queue = await asyncio.wait_for(
        _hermes_answers_after(monkeypatch, tmp_path, [{"ok": False, **record}], text=_SERVER_ERROR_TEXT), 20
    )
    (event,) = queue.events
    if mode == "serve":  # hermes' reply, byte for byte, as before
        assert isinstance(event, Message) and _text(event) == _SERVER_ERROR_TEXT
        return
    assert isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_FAILED
    assert _text(event.status.message) == (
        f"the turn's last model call failed ({reason}; custom minimax/MiniMax-M2.7): {_SERVER_ERROR_TEXT}"
    )
    result = classify_turn(queue.events)
    assert (result.status, result.output, result.exit_code) == ("failed", "", 1)
    assert result.error.startswith(f"the turn's last model call failed ({reason}")


@pytest.mark.asyncio
async def test_a_failed_model_call_hermes_gave_no_reason_for(monkeypatch, tmp_path):
    # No reason and no status: the failure is still the run's.
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    queue = await asyncio.wait_for(
        _hermes_answers_after(monkeypatch, tmp_path, [{"ok": False}], text=_SERVER_ERROR_TEXT), 20
    )
    (event,) = queue.events
    assert isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_FAILED
    assert _text(event.status.message) == (
        f"the turn's last model call failed (custom minimax/MiniMax-M2.7): {_SERVER_ERROR_TEXT}"
    )


NOT_FAILURES = {
    # A retry or a fallback provider answered after the failure.
    "recovered": dict(records=[{"ok": False, "status": 429, "reason": "rate_limit"}, {"ok": True}]),
    "recovered-from-500": dict(records=[{"ok": False, "status": 500, "reason": "server_error"}, {"ok": True}]),
    # Another turn's failure, before this one started.
    "earlier-turn": dict(records=[], before_turn=[{"ok": False, "status": 402, "reason": "billing", "at": 1.0}]),
    # A call that answered decides, whatever else the record holds (review 21723
    # N1: the plugin never writes a failure on an `ok: true` record, so only this
    # pins the executor's `ok is not False` guard).
    "answered-with-refusal-fields": dict(records=[{"ok": True, "status": 429, "reason": "rate_limit"}]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(NOT_FAILURES))
async def test_hermes_answer_stays_an_answer_unless_the_last_call_failed(monkeypatch, tmp_path, case):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    queue = await asyncio.wait_for(
        _hermes_answers_after(monkeypatch, tmp_path, text="the weekly report", **NOT_FAILURES[case]), 20
    )
    (event,) = queue.events
    assert isinstance(event, Message) and _text(event) == "the weekly report"
    assert classify_turn(queue.events).status == "succeeded"


# --- a turn that ended with no answer ----------------------------------------------
#
# A scheduled task may finish quietly on purpose (a bare [SILENT]): hermes then
# sends nothing under the turn's id and the turn settles with the runtime's
# no-response text. That is a succeeded run WHEN a model call answered during
# the turn. In a run pod, whose gateway records every model call, a turn that
# ends with no answer and no recorded call never reached the model at all.


async def _silent_end_after(monkeypatch, tmp_path, records=(), *, before_turn=()):
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv("MOLECULE_MODEL_CALL_FILE", str(model_calls))
    for record in before_turn:
        _model_call(model_calls, **record)
    async with _running(monkeypatch, HERMES_TURN_SETTLE_SECONDS="0.3") as (ex, gateway):
        queue = _Queue()
        turn = asyncio.create_task(ex.execute(_run_ctx(), queue))
        await _dispatched(gateway, SPEC.prompt)
        for record in records:
            _model_call(model_calls, **record)
        await turn
    return queue


@pytest.mark.asyncio
async def test_a_quiet_finish_after_a_model_call_that_answered_is_a_succeeded_run(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    queue = await asyncio.wait_for(_silent_end_after(monkeypatch, tmp_path, [{"ok": True}]), 20)
    (event,) = queue.events
    assert isinstance(event, Message) and _text(event) == executor_mod._NO_RESPONSE
    assert classify_turn(queue.events).status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("before_turn", [(), ({"ok": True, "at": 1.0},)], ids=["no-record", "only-an-earlier-turns"])
async def test_no_answer_and_no_model_call_is_a_failed_run(monkeypatch, tmp_path, before_turn):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    queue = await asyncio.wait_for(_silent_end_after(monkeypatch, tmp_path, before_turn=before_turn), 20)
    (event,) = queue.events
    assert isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_FAILED
    assert _text(event.status.message) == (
        "the turn ended with no answer, and no model call was recorded for it"
    )
    result = classify_turn(queue.events)
    assert (result.status, result.output, result.exit_code) == ("failed", "", 1)


@pytest.mark.asyncio
async def test_no_answer_after_a_failed_model_call_names_the_failed_call(monkeypatch, tmp_path):
    # Retries exhausted on empty responses, and hermes sent nothing.
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    queue = await asyncio.wait_for(
        _silent_end_after(monkeypatch, tmp_path, [{"ok": False, "reason": "invalid_response"}]), 20
    )
    (event,) = queue.events
    assert isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_FAILED
    assert _text(event.status.message) == (
        "the turn's last model call failed (invalid_response; custom minimax/MiniMax-M2.7): "
        + executor_mod._NO_RESPONSE
    )


@pytest.mark.asyncio
async def test_a_quiet_finish_outside_a_run_is_unchanged(monkeypatch, tmp_path):
    # Serve mode: a workspace's quiet turn stays a reply, whatever was recorded.
    queue = await asyncio.wait_for(_silent_end_after(monkeypatch, tmp_path), 20)
    (event,) = queue.events
    assert isinstance(event, Message) and _text(event) == executor_mod._NO_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["serve", "once"])
async def test_a_turn_the_provider_refused_on_the_chat_completions_fallback(monkeypatch, tmp_path, mode):
    # hermes' API server answers a failed turn with finish_reason "error" and
    # its explanation as the content; the plugin's record says why.
    if mode == "once":
        monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv("MOLECULE_MODEL_CALL_FILE", str(model_calls))

    async def refused(_request):
        _model_call(model_calls, ok=False, status=402, reason="billing")
        return web.json_response({"choices": [{"finish_reason": "error", "message": {
            "role": "assistant", "content": _BILLING_TEXT}}]})

    async with _chat_completions(monkeypatch, refused) as ex:
        queue = _Queue()
        await asyncio.wait_for(ex.execute(_run_ctx(), queue), 20)
    (event,) = queue.events
    if mode == "serve":
        assert isinstance(event, Message) and _text(event) == _BILLING_TEXT
        return
    assert isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_FAILED
    assert _text(event.status.message).startswith("the model provider refused the call (billing, HTTP 402")


@pytest.mark.asyncio
async def test_a_failed_model_call_on_the_chat_completions_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv("MOLECULE_MODEL_CALL_FILE", str(model_calls))

    async def failing(_request):
        _model_call(model_calls, ok=False, status=503, reason="overloaded")
        return web.json_response({"choices": [{"finish_reason": "error", "message": {
            "role": "assistant", "content": _SERVER_ERROR_TEXT}}]})

    async with _chat_completions(monkeypatch, failing) as ex:
        queue = _Queue()
        await asyncio.wait_for(ex.execute(_run_ctx(), queue), 20)
    (event,) = queue.events
    assert isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_FAILED
    assert _text(event.status.message).startswith("the turn's last model call failed (overloaded, HTTP 503")


def test_the_refusal_reasons_are_hermes_own():
    # Pinned to hermes' FailoverReason values at HERMES_COMMIT (agent/error_classifier.py).
    assert executor_mod._PROVIDER_REFUSAL_REASONS == {
        "billing", "rate_limit", "upstream_rate_limit", "auth", "auth_permanent", "model_entitlement",
    }
    assert executor_mod._PROVIDER_REFUSAL_STATUSES == {401, 402, 403, 429}


# --- a Light run end to end ------------------------------------------------------


def test_a_light_runs_turn_gets_the_run_deadline(monkeypatch):
    # start.sh exports the run's deadline as HERMES_TURN_REPLY_DEADLINE_SECONDS;
    # a run's turn is the agent's own scheduled turn, not a short-fuse self-wake.
    monkeypatch.setenv("HERMES_TURN_REPLY_DEADLINE_SECONDS", "600")
    ex = _executor(monkeypatch, FakeGateway())
    caller = ex._caller_of(_run_ctx(), SPEC.prompt)
    assert (caller.origin, caller.deadline, caller.wake) == ("self", 600.0, "self-scheduler 'daily-report'")


class _Ledger:
    """Records the run ledger writes run_once makes."""

    def __init__(self) -> None:
        self.bodies: list = []

    async def __call__(self, _spec, body) -> int:
        self.bodies.append(body)
        return 200


def _spec() -> RunSpec:
    return dataclasses.replace(SPEC, deadline_seconds=60, started_at=time.time(), started_monotonic=time.monotonic())


@pytest.mark.asyncio
async def test_once_mode_records_a_stalled_hermes_turn_as_failed(monkeypatch):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    hb = SimpleNamespace(active_tasks=0, current_task="")
    ledger = _Ledger()
    async with _running(monkeypatch, heartbeat=hb, HERMES_TURN_STALL_SECONDS="0.3") as (ex, _gateway):
        rc = await asyncio.wait_for(
            run_once(_spec(), ex, heartbeat=hb, report=ledger, settle_poll_seconds=0.02), 20
        )
    assert rc == 1
    assert [(b["phase"], b["status"]) for b in ledger.bodies] == [("started", "running"), ("finished", "failed")]
    assert "no activity from the hermes gateway" in ledger.bodies[-1]["error"]
    assert (ledger.bodies[-1]["exit_code"], ledger.bodies[-1]["output"]) == (1, "")
    assert hb.active_tasks == 0


@pytest.mark.asyncio
async def test_once_mode_records_a_hermes_answer_as_succeeded(monkeypatch):
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    hb = SimpleNamespace(active_tasks=0, current_task="")
    ledger = _Ledger()
    async with _running(monkeypatch, heartbeat=hb) as (ex, gateway):
        run = asyncio.create_task(run_once(_spec(), ex, heartbeat=hb, report=ledger, settle_poll_seconds=0.02))
        await _dispatched(gateway, SPEC.prompt)
        await gateway.answer(SPEC.prompt, "the weekly report")
        rc = await asyncio.wait_for(run, 20)
    assert rc == 0
    assert [(b["phase"], b["status"]) for b in ledger.bodies] == [("started", "running"), ("finished", "succeeded")]
    assert ledger.bodies[-1]["output"] == "the weekly report"


@pytest.mark.asyncio
async def test_once_mode_records_a_provider_refusal_as_failed(monkeypatch, tmp_path):
    # The Phase 1 gate's run, end to end: was `succeeded` with hermes' text.
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv("MOLECULE_MODEL_CALL_FILE", str(model_calls))
    hb = SimpleNamespace(active_tasks=0, current_task="")
    ledger = _Ledger()
    async with _running(monkeypatch, heartbeat=hb) as (ex, gateway):
        run = asyncio.create_task(run_once(_spec(), ex, heartbeat=hb, report=ledger, settle_poll_seconds=0.02))
        await _dispatched(gateway, SPEC.prompt)
        _model_call(model_calls, ok=False, status=402, reason="billing")
        await gateway.answer(SPEC.prompt, _BILLING_TEXT)
        rc = await asyncio.wait_for(run, 20)
    assert rc == 1
    assert [(b["phase"], b["status"]) for b in ledger.bodies] == [("started", "running"), ("finished", "failed")]
    finished = ledger.bodies[-1]
    assert (finished["exit_code"], finished["output"]) == (1, "")
    assert finished["error"].startswith("the model provider refused the call (billing, HTTP 402")
    assert "account entitlement is exhausted" in finished["error"]
    assert hb.active_tasks == 0


@pytest.mark.asyncio
async def test_once_mode_records_a_failed_model_call_as_failed(monkeypatch, tmp_path):
    # A provider outage, end to end: was `succeeded` with hermes' error text.
    monkeypatch.setenv("MOLECULE_RUN_MODE", "once")
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv("MOLECULE_MODEL_CALL_FILE", str(model_calls))
    hb = SimpleNamespace(active_tasks=0, current_task="")
    ledger = _Ledger()
    async with _running(monkeypatch, heartbeat=hb) as (ex, gateway):
        run = asyncio.create_task(run_once(_spec(), ex, heartbeat=hb, report=ledger, settle_poll_seconds=0.02))
        await _dispatched(gateway, SPEC.prompt)
        _model_call(model_calls, ok=False, status=500, reason="server_error")
        await gateway.answer(SPEC.prompt, _SERVER_ERROR_TEXT)
        rc = await asyncio.wait_for(run, 20)
    assert rc == 1
    assert [(b["phase"], b["status"]) for b in ledger.bodies] == [("started", "running"), ("finished", "failed")]
    finished = ledger.bodies[-1]
    assert (finished["exit_code"], finished["output"]) == (1, "")
    assert finished["error"].startswith("the turn's last model call failed (server_error, HTTP 500")
    assert "upstream connect error" in finished["error"]
    assert hb.active_tasks == 0


# --- the switch and the sites ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,want", [(None, False), ("", False), ("once", True), (" ONCE ", True), ("twice", False)]
)
def test_run_once_mode_follows_the_runtime_switch(monkeypatch, raw, want):
    if raw is not None:
        monkeypatch.setenv("MOLECULE_RUN_MODE", raw)
    assert executor_mod._run_once_mode() is want


class _Sites(ast.NodeVisitor):
    """Count, per enclosing function, the calls that enqueue an event and the
    calls of _give_up."""

    def __init__(self) -> None:
        self.stack: list = []
        self.enqueue: collections.Counter = collections.Counter()
        self.give_up: collections.Counter = collections.Counter()

    def visit_FunctionDef(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "enqueue_event":
            self.enqueue[self.stack[-1]] += 1
        if isinstance(node.func, ast.Name) and node.func.id == "_give_up":
            self.give_up[self.stack[-1]] += 1
        self.generic_visit(node)


def test_every_reply_the_executor_composes_goes_through_give_up():
    """A new reply site must be classified: hermes' answer, or a give-up."""
    sites = _Sites()
    sites.visit(ast.parse((ROOT / "executor.py").read_text(encoding="utf-8")))
    # Direct enqueues: _give_up itself, and hermes' answer on each transport.
    assert sites.enqueue == {"_give_up": 2, "_run_plugin_turn": 1, "_execute_via_chat_completions": 1}
    assert sites.give_up == {
        "execute": 2,                        # P1 empty prompt, P2 repeat wake dropped
        "_dispatch_and_wait": 2,             # P3 queue full, P4 shutting down
        "_await_outcome": 4,                 # P5 interim, P6 cancelled, P7 abandoned, P8 other error
        "_run_plugin_turn": 2,               # P9 overflow the heal could not clear, P10 the last model
                                             # call failed, or no answer and no model call
        "_execute_via_chat_completions": 4,  # C4 as P10, C3 tool-round cap, C1 HTTP error, C2 unreachable
    }


def test_this_file_runs_in_the_required_gate():
    steps = yaml.safe_load((ROOT / ".gitea/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]["conformance"]["steps"]
    (script,) = [s.get("run", "") for s in steps if s.get("name") == "Run adapter and private-runtime contracts"]
    assert "tests/test_run_once_failure_signal.py" in script.split()
