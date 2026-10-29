"""End-to-end validation of the executor's plugin path against a real
``hermes gateway run`` subprocess + a stub LLM.

This is the highest-fidelity local approximation of staging E2E. It
proves every hop in the production chain except the platform-side
peer-message routing:

    HermesAgentProxyExecutor.execute()
        → POST to real hermes plugin /a2a/inbound
            → hermes dispatches MessageEvent through full pipeline
                → hermes calls our stub OpenAI-compat /v1/chat/completions
                ← stub returns deterministic text
            ← hermes plugin's send() POSTs reply to executor's callback
        ← executor's pending Future resolves
    ← execute() emits text on event_queue

Pre-reqs:
    - hermes installed the way the image installs it:
      ``bash vendor/hermes-agent/install.sh --skip-setup --commit <HERMES_COMMIT>``
      then ``scripts/hermes-post-install.sh`` (the molecule-a2a plugin into
      hermes' dependency environment)
    - This template's executor.py + adapter.py importable, with the pinned
      molecules-workspace-runtime installed in the interpreter running this

The gateway runs under a throwaway HOME/HERMES_HOME, like the image's gateway
runs under the /configs volume, so this applies the same bridge start.sh
applies: ``<HERMES_HOME>/installs`` links to the install's own, and
HERMES_RUNTIME_DIR / HERMES_DISABLE_LAZY_INSTALLS=1 are set, unless the caller
already set them. Without it hermes >= v0.22 refuses to start ("no dependency
environment is committed for this install").

Run:
    python3 scripts/e2e_full_chain.py     # HERMES_BIN defaults to ~/.local/bin/hermes
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_HERMES_BIN = str(Path.home() / ".local" / "bin" / "hermes")
# The data root the installer used; it holds hermes' tool store and the
# selection of its dependency environment.
INSTALL_HOME = Path(os.environ.get("HERMES_INSTALL_HOME", Path.home() / ".hermes"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_url(url: str, timeout_secs: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.25)
        except Exception:
            time.sleep(0.25)
    return False


TOOL_MARKER = "use one tool"
# "[e2e:sleep:<seconds>:<calls>] <tag>": the stub answers with <calls> terminal
# calls of `sleep <seconds>`, one after another, then the text "slept:<tag>".
# That gives a turn of a known length made of real hermes tool calls.
SLEEP_RE = re.compile(r"\[e2e:sleep:(\d+(?:\.\d+)?):(\d+)\]\s*(\S+)")
# "[e2e:silent]": the stub answers with a bare silence marker.
SILENT_MARKER = "[e2e:silent]"


def _tool_call_for(tools: list, probe: str) -> tuple[str, dict] | None:
    """A cheap, side-effect-free call to a tool the gateway offered."""
    names = {((t or {}).get("function") or {}).get("name") for t in tools or []}
    if "read_file" in names:
        return "read_file", {"path": probe}
    if "terminal" in names:
        return "terminal", {"command": "echo e2e-tool"}
    return None


async def _stub_llm_server(port: int, observed: dict):
    """Tiny OpenAI-compat /v1/chat/completions server that echoes the
    last user message back as the assistant content. Lets us verify
    the round trip without needing a real LLM key.

    A user message containing TOOL_MARKER gets one tool call first (when
    the request offers a usable tool); once the tool result is in the
    transcript it answers ``tool-done``. ``observed`` records what the
    gateway offered and called, for the assertions and the failure report."""
    from aiohttp import web

    def _text(message: dict) -> str:
        content = message.get("content")
        return content if isinstance(content, str) else json.dumps(content)

    async def chat_completions(request):
        body = await request.json()
        messages = body.get("messages", [])
        last_user = next(
            (_text(m) for m in reversed(messages) if m.get("role") == "user"), ""
        )
        # Echo with a tag so we can match the trip.
        reply = f"echo[{last_user}]"
        tool_call = None
        observed.setdefault("user_texts", []).append(last_user)
        observed.setdefault("requests", []).append((time.monotonic(), last_user))
        last_user_at = max(
            (i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1
        )
        sleep = SLEEP_RE.search(last_user)
        # Only the turn being answered decides: the whole session is resent on
        # every request, and an earlier tool turn must not shape a later one.
        marker_at = last_user_at if TOOL_MARKER in last_user else -1
        if sleep and body.get("tools"):
            seconds, calls, tag = sleep.group(1), int(sleep.group(2)), sleep.group(3)
            done = sum(1 for m in messages[last_user_at + 1:] if m.get("role") == "tool")
            offered = {((t or {}).get("function") or {}).get("name") for t in body["tools"]}
            if done < calls and "terminal" in offered:
                observed.setdefault("sleeps", []).append((tag, done + 1, time.monotonic()))
                tool_call = ("terminal", {"command": f"sleep {seconds} && echo slept-{tag}-{done + 1}"})
            elif "terminal" not in offered:
                reply = "no-terminal-tool"
            else:
                reply = f"slept:{tag}"
        elif SILENT_MARKER in last_user:
            reply = "[SILENT]"
        elif marker_at >= 0 and body.get("tools"):
            observed["offered"] = sorted(
                ((t or {}).get("function") or {}).get("name") or "?" for t in body["tools"]
            )
            if any(m.get("role") == "tool" for m in messages[marker_at + 1:]):
                # Give the runtime's activity watcher a poll or two inside the turn.
                await asyncio.sleep(1.0)
                reply = "tool-done"
            else:
                tool_call = _tool_call_for(body["tools"], observed["probe"])
                if tool_call is not None:
                    observed["called"] = tool_call[0]
        if tool_call is not None:
            name, args = tool_call
            observed["call_seq"] = observed.get("call_seq", 0) + 1
            calls = [{
                "index": 0, "id": f"call_e2e_{observed['call_seq']}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]
            if body.get("stream"):
                resp = web.StreamResponse(
                    status=200, headers={"Content-Type": "text/event-stream"}
                )
                await resp.prepare(request)
                for choice in (
                    {"index": 0, "delta": {"role": "assistant", "content": None, "tool_calls": calls},
                     "finish_reason": None},
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"},
                ):
                    chunk = {
                        "id": "chatcmpl-test", "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": body.get("model", "test"),
                        "choices": [choice],
                    }
                    await resp.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
                return resp
            message_calls = [{k: v for k, v in c.items() if k != "index"} for c in calls]
            return web.json_response({
                "id": "chatcmpl-test", "object": "chat.completion",
                "created": int(time.time()), "model": body.get("model", "test"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": None, "tool_calls": message_calls},
                    "finish_reason": "tool_calls",
                }],
            })
        if body.get("stream"):
            # hermes asks for stream=true; a plain JSON body is an empty
            # stream to it (EmptyStreamError), so answer in SSE chunks.
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await resp.prepare(request)
            created = int(time.time())
            model = body.get("model", "test")

            def chunk(delta, finish=None):
                return {
                    "id": "chatcmpl-test", "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }

            for obj in (
                chunk({"role": "assistant", "content": ""}),
                chunk({"content": reply}),
                chunk({}, "stop"),
            ):
                await resp.write(("data: " + json.dumps(obj) + "\n\n").encode())
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        return web.json_response({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "test"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
        })

    async def models(_request):
        # Some hermes versions probe /v1/models on first use.
        return web.json_response({
            "object": "list",
            "data": [{"id": "test-model", "object": "model"}],
        })

    # Stand-ins for the two platform endpoints the executor calls on its own:
    # /notify (late replies) and /registry/heartbeat (busy pushes).
    async def notify(request):
        observed.setdefault("notified", []).append(await request.json())
        return web.json_response({"ok": True})

    async def heartbeat(_request):
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/workspaces/{workspace_id}/notify", notify)
    app.router.add_post("/registry/heartbeat", heartbeat)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site


def _ctx(text: str, *, task_id: str = "task-fullchain", source_type: str | None = None):
    text_part = MagicMock()
    text_part.text = text
    text_part.kind = "text"
    msg = MagicMock()
    msg.task_id = task_id
    msg.message = None  # else extract_message_text() returns "" and execute() never dispatches
    msg.parts = [text_part]
    ctx = MagicMock()
    ctx.task_id = task_id
    ctx.message = msg
    if source_type:
        # Where the runtime's own self-wakes carry it: the request metadata.
        msg.metadata = None
        ctx.metadata = {"source_type": source_type}
    return ctx


class _LogTap(logging.Handler):
    """Collects the executor's log lines so a scenario can assert on them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []
        self.times: List[float] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
        self.times.append(time.monotonic())

    def matching(self, pattern: str) -> List[str]:
        return [ln for ln in self.lines if re.search(pattern, ln)]

    def first_at(self, pattern: str) -> float | None:
        """When a line matching ``pattern`` was first logged, or None."""
        return next(
            (at for at, ln in zip(self.times, self.lines) if re.search(pattern, ln)), None
        )


async def _until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


class _CapturingQueue:
    def __init__(self):
        self.events: List[Any] = []
        self.at: List[float] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)
        self.at.append(time.monotonic())


def _texts(queue: "_CapturingQueue") -> List[str]:
    return ["".join(p.text for p in e.parts if getattr(p, "text", None)) for e in queue.events]


async def _turn_supervision_scenarios(
    executor, executor_mod, heartbeat, tap, chips, observed, proc, platform_url
) -> str | None:
    """Turn supervision against the real gateway. Returns a failure, or None.

    Each scenario is a production failure from 2026-10-29 (jrs) or an open
    issue, replayed through the pinned hermes with a stub model whose turns
    are real terminal calls of known length.
    """
    from molecule_runtime.runtime_inbox import any_turn_in_flight

    os.environ["PLATFORM_URL"] = platform_url
    base_policy = executor._turn_policy

    def policy(**changes):
        """The production policy with this scenario's changes, and only those."""
        executor._turn_policy = dataclasses.replace(
            base_policy, poll_seconds=0.2, control_seconds=15.0, **changes
        )

    def sleeps(tag):
        return [s for s in observed.get("sleeps", []) if s[0] == tag]

    def reached_model(fragment):
        """When the gateway first asked the model about ``fragment``, or None."""
        return next((at for at, text in observed.get("requests", []) if fragment in text), None)

    def relayed(fragment):
        return [n for n in observed.get("notified", []) if fragment in str(n.get("message", ""))]

    def idle():
        return heartbeat.active_tasks == 0 and not any_turn_in_flight()

    # A. One 12 s tool call against an 8 s stall window. Nothing touches the
    # activity file until the call returns, so only the gateway's own
    # "still working" sends can keep the turn alive. The old executor gave up
    # on every turn at 600 s whatever it was doing.
    policy(stall_seconds=8.0, max_seconds=0.0, self_reply_deadline_seconds=2.0)
    mark = len(tap.lines)
    long_q = _CapturingQueue()
    started = time.monotonic()
    long_turn = asyncio.create_task(
        executor.execute(_ctx("[e2e:sleep:12:1] long", task_id="t-long"), long_q)
    )
    if not await _until(lambda: sleeps("long"), 30):
        return f"the stub never got the long turn's tool request: offered={observed.get('offered')!r}"
    if not await _until(lambda: heartbeat.active_tasks == 1 and any_turn_in_flight(), 5):
        return (f"busy not reported during the turn: active_tasks={heartbeat.active_tasks} "
                f"in_flight={any_turn_in_flight()}")
    print("OK: busy reported while the turn runs (active_tasks=1, is_busy)")

    # B. Self-wakes that land mid-turn. A routine one (the idle digest) is
    # fast-acked and dropped: production queued 32 of these behind one turn and
    # ran them for hours. Delegation results are the agent's only copy of them,
    # so they wait their turn instead, and the harvester is answered at the
    # self-turn deadline, before its own 120 s request gives up.
    wake_q = _CapturingQueue()
    wake_at = time.monotonic()
    await asyncio.wait_for(
        executor.execute(
            _ctx("<SYSTEM IDLE PROMPT> e2e-wake", task_id="t-wake", source_type="self-idle"),
            wake_q,
        ),
        5,
    )
    if _texts(wake_q) != [executor_mod._SELF_PING_SKIPPED]:
        return f"a self-idle wake during a turn was not fast-acked: {_texts(wake_q)!r}"
    print(f"OK: a self-idle wake during the turn was fast-acked in {time.monotonic() - wake_at:.2f}s")

    harvest_q = _CapturingQueue()
    harvest_at = time.monotonic()
    try:
        await asyncio.wait_for(
            executor.execute(
                _ctx("Delegation results are ready: e2e-harvest", task_id="t-harvest",
                     source_type="self-harvester"),
                harvest_q,
            ),
            10,
        )
    except asyncio.TimeoutError:
        return "the harvester's wake was not answered at the self-turn deadline"
    harvest_waited = time.monotonic() - harvest_at
    if len(_texts(harvest_q)) != 1 or not _texts(harvest_q)[0].startswith("[autonomous "):
        return f"harvester wake answer: {_texts(harvest_q)!r}"
    if reached_model("e2e-harvest") is not None:
        return "the delegation results reached the model while another turn held the gateway"
    print(
        "OK: delegation results that arrived mid-turn were queued, and the harvester was "
        f"answered in {harvest_waited:.1f}s (self-turn deadline 2 s)"
    )

    try:
        await asyncio.wait_for(long_turn, 60)
    except asyncio.TimeoutError:
        return "the 12 s turn did not finish within 60 s"
    elapsed = time.monotonic() - started
    if _texts(long_q) != ["slept:long"]:
        return f"long turn answer: {_texts(long_q)!r}"
    if elapsed < 11:
        return f"the long turn finished too fast to prove anything ({elapsed:.1f}s)"
    # The long turn is the only one this scenario's caller waited out.
    progress = [
        int(m.group(1))
        for m in (re.search(r"answered after .*\((\d+) progress sends\)", ln) for ln in tap.lines[mark:])
        if m
    ]
    if not progress or progress[0] < 1:
        return f"no gateway progress sends reached the executor during a {elapsed:.0f}s turn"
    if any("e2e-wake" in text for text in observed.get("user_texts", [])):
        return "the dropped self-wake reached the model anyway"
    if not await _until(
        lambda: any(name == "terminal" and "sleep 12" in (summary or "") for name, summary, _ in chips),
        5,
    ):
        activity_file = os.environ.get("MOLECULE_TOOL_ACTIVITY_FILE", "")
        try:
            record = Path(activity_file).read_text(encoding="utf-8")[:400]
        except OSError as exc:
            record = f"<unreadable: {exc}>"
        return (f"the gateway's tool call never became an activity row: chips={chips!r} "
                f"activity file={record!r}")
    print(
        f"OK: a single 12 s tool call outlived the 8 s stall window ({elapsed:.1f}s, "
        f"{progress[0]} progress sends); its tool call became an activity row"
    )

    # The queued delegation results run once the long turn is answered, as a
    # turn of their own. Their answer is the agent's business: it reports
    # results with its own tools, so nothing is relayed to the chat.
    if not await _until(lambda: reached_model("e2e-harvest") is not None, 30):
        return "the queued delegation results never reached the model"
    if reached_model("e2e-harvest") < long_q.at[0]:
        return "the delegation results reached the model before the running turn was answered"
    if not await _until(lambda: tap.matching(r"late reply .* source=self-harvester — not relayed"), 30):
        return "the delegation-results turn was never answered"
    if relayed("e2e-harvest"):
        return "a delegation-results answer was relayed to the user's chat"
    if not await _until(idle, 5):
        return f"busy not released after the turns: active_tasks={heartbeat.active_tasks}"
    print("OK: the queued delegation results then ran as their own turn; busy released")

    # C. hermes#393. A message sent while a turn ran used to wait in hermes' own
    # queue, run inside that turn, and come back under the running turn's id.
    # Now it waits here, reaches the gateway once the running turn is answered,
    # and gets its own answer. A command sent mid-turn still goes straight
    # through, and hermes answers it under its own id.
    policy(stall_seconds=30.0, max_seconds=0.0)
    x_q, y_q, status_q = _CapturingQueue(), _CapturingQueue(), _CapturingQueue()
    x = asyncio.create_task(executor.execute(_ctx("[e2e:sleep:4:1] X", task_id="t-x"), x_q))
    if not await _until(lambda: sleeps("X"), 30):
        return "X's tool call never started"
    y = asyncio.create_task(executor.execute(_ctx("reply with Y-OK", task_id="t-y"), y_q))
    status_at = time.monotonic()
    try:
        await asyncio.wait_for(executor.execute(_ctx("/status", task_id="t-status"), status_q), 10)
    except asyncio.TimeoutError:
        return "/status sent mid-turn was not answered while the turn ran"
    status_in = time.monotonic() - status_at
    status_text = (_texts(status_q) or [""])[0]
    if x.done() or not status_text.strip() or "hermes plugin" in status_text:
        return f"/status mid-turn: answer={_texts(status_q)!r} X done={x.done()}"
    try:
        await asyncio.wait_for(asyncio.gather(x, y), 60)
    except asyncio.TimeoutError:
        return f"X/Y did not both finish: X={_texts(x_q)!r} Y={_texts(y_q)!r}"
    if _texts(x_q) != ["slept:X"] or _texts(y_q) != ["echo[reply with Y-OK]"]:
        return f"queued message answers misrouted: X={_texts(x_q)!r} Y={_texts(y_q)!r}"
    y_reached = reached_model("reply with Y-OK")
    if y_reached is None or y_reached < x_q.at[0]:
        return "Y reached the model before X was answered"
    if not await _until(idle, 5):
        return "busy not released after X and Y"
    print(
        "OK: a message sent mid-turn waited for the running turn, then got its own answer; "
        f"/status sent mid-turn was answered in {status_in:.1f}s ({status_text[:40]!r}…)"
    )

    # D. The cap ends a real run, and the message waiting behind it still gets
    # a run of its own: /stop goes out, the run makes no more tool calls, and the
    # waiting message reaches the gateway once /stop is answered. Hermes starts
    # it only when the stopped run lets go of the session (its tool call in
    # flight ends first), and prefixes it with a note about the interrupted turn.
    policy(stall_seconds=30.0, max_seconds=4.0)
    cap_q, after_q = _CapturingQueue(), _CapturingQueue()
    capped = asyncio.create_task(
        executor.execute(_ctx("[e2e:sleep:3:10] capped", task_id="t-cap"), cap_q)
    )
    if not await _until(lambda: sleeps("capped"), 30):
        return "the capped turn never started"
    after = asyncio.create_task(executor.execute(_ctx("after cap", task_id="t-after"), after_q))
    try:
        await asyncio.wait_for(capped, 30)
    except asyncio.TimeoutError:
        return "the turn cap did not end a 30 s turn"
    # The cap was for the capped turn; the production cap is an hour.
    policy(stall_seconds=30.0, max_seconds=0.0)
    if len(_texts(cap_q)) != 1 or "turn cap" not in _texts(cap_q)[0]:
        return f"capped turn answer: {_texts(cap_q)!r}"
    stop_answered = tap.first_at(r"/stop on .* answered")
    if stop_answered is None:
        return "the gateway never answered /stop"
    calls_at_stop = len(sleeps("capped"))
    try:
        await asyncio.wait_for(after, 20)
    except asyncio.TimeoutError:
        return "the message waiting behind the capped turn was never answered"
    after_text = (_texts(after_q) or [""])[0]
    if len(_texts(after_q)) != 1 or not (after_text.startswith("echo[") and after_text.endswith("after cap]")):
        return f"message waiting behind the cap: {_texts(after_q)!r}"
    after_reached = reached_model("after cap")
    if after_reached is None or after_reached < stop_answered:
        return "the waiting message reached the gateway before /stop was answered"
    await asyncio.sleep(8)
    if len(sleeps("capped")) > calls_at_stop + 1:  # the call in flight may still complete
        return f"the capped run kept calling tools after /stop: {sleeps('capped')!r}"
    if relayed("slept:capped"):
        return "the stopped run still delivered an answer"
    if not await _until(idle, 5):
        return "busy not released after the capped turn"
    print(
        "OK: the turn cap sent /stop, the run made no further tool calls, and the message "
        "waiting behind it got its own answer once /stop was answered"
    )

    # F. A turn that outlasts its caller. The platform gives up on an A2A
    # request at 30 minutes and delivers it again, so the caller is answered
    # first; the turn runs on, the workspace stays busy, and the answer is
    # relayed to the chat when it comes.
    policy(stall_seconds=30.0, max_seconds=0.0, reply_deadline_seconds=3.0)
    slow_q = _CapturingQueue()
    slow_at = time.monotonic()
    try:
        await asyncio.wait_for(
            executor.execute(_ctx("[e2e:sleep:3:2] slow", task_id="t-slow"), slow_q), 15
        )
    except asyncio.TimeoutError:
        return "the caller was not answered at the reply deadline"
    answered_in = time.monotonic() - slow_at
    if len(_texts(slow_q)) != 1 or "still working on this" not in _texts(slow_q)[0]:
        return f"reply-deadline answer: {_texts(slow_q)!r}"
    if answered_in > 6:
        return f"the caller waited {answered_in:.1f}s past a 3 s reply deadline"
    if heartbeat.active_tasks != 1 or not any_turn_in_flight():
        return "busy was released when the caller was answered, before the turn ended"
    if not await _until(lambda: relayed("slept:slow"), 30):
        return f"the turn's late answer never reached the chat: notified={observed.get('notified')!r}"
    if not await _until(idle, 5):
        return "busy not released after the late answer"
    print(
        f"OK: the caller was answered at the 3 s reply deadline ({answered_in:.1f}s); the "
        "turn ran on as busy and its answer was relayed to the chat"
    )

    # G. A turn hermes ends silently: the model answers with a bare silence
    # marker, which hermes drops on these internal turns, so nothing comes back
    # under the turn's id. It is settled once the gateway has been idle for the
    # settle window, instead of holding the chat for the stall window.
    policy(stall_seconds=120.0, max_seconds=0.0)
    settle = base_policy.settle_seconds
    if not settle:
        return "the settle window is off although HERMES_AGENT_NOTIFY_INTERVAL is set"
    silent_q = _CapturingQueue()
    silent_at = time.monotonic()
    try:
        await asyncio.wait_for(
            executor.execute(_ctx(f"{SILENT_MARKER} nothing to say", task_id="t-silent"), silent_q),
            settle + 30,
        )
    except asyncio.TimeoutError:
        return f"a silent turn was not settled within {settle + 30:g}s"
    silent_for = time.monotonic() - silent_at
    if _texts(silent_q) != [executor_mod._NO_RESPONSE]:
        return f"silent turn answer: {_texts(silent_q)!r}"
    if not await _until(idle, 5):
        return "busy not released after the silent turn"
    print(
        f"OK: a turn hermes ended silently was settled after {silent_for:.1f}s "
        f"(settle window {settle:g}s, stall window 120 s)"
    )
    # A run hermes starts itself (a notify_on_complete background job ending)
    # is not tested here: the pinned gateway refuses that wake for molecule-a2a
    # chats ("Watch notification not admitted ...: not_found",
    # hermes-platform-molecule-a2a#14). tests/test_turn_supervision.py covers
    # what the executor does with such a run's answer.

    # E. A gateway that dies mid-turn: the turn is abandoned one stall window
    # later, and the message waiting behind it fails at once rather than
    # waiting out a stall window of its own.
    policy(stall_seconds=4.0, max_seconds=0.0)
    doomed_q, behind_q = _CapturingQueue(), _CapturingQueue()
    doomed = asyncio.create_task(
        executor.execute(_ctx("[e2e:sleep:30:1] doomed", task_id="t-doomed"), doomed_q)
    )
    if not await _until(lambda: sleeps("doomed"), 30):
        return "the doomed turn never started"
    behind = asyncio.create_task(
        executor.execute(_ctx("behind doomed", task_id="t-behind"), behind_q)
    )
    await asyncio.sleep(0.5)
    if behind.done() or reached_model("behind doomed") is not None:
        return f"the second message did not wait behind the running turn: {_texts(behind_q)!r}"
    proc.kill()
    proc.wait(timeout=10)
    killed_at = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.gather(doomed, behind), 30)
    except asyncio.TimeoutError:
        return (f"turns on a dead gateway were not settled: doomed={_texts(doomed_q)!r} "
                f"behind={_texts(behind_q)!r}")
    waited = doomed_q.at[0] - killed_at
    if len(_texts(doomed_q)) != 1 or "no activity from the hermes gateway" not in _texts(doomed_q)[0]:
        return f"dead-gateway turn answer: {_texts(doomed_q)!r}"
    if len(_texts(behind_q)) != 1 or "POST error" not in _texts(behind_q)[0]:
        return f"the message behind the dead turn: {_texts(behind_q)!r}"
    behind_after = behind_q.at[0] - doomed_q.at[0]
    if behind_after > 2:
        return f"the message behind the dead turn waited {behind_after:.1f}s more"
    if not await _until(idle, 5):
        return "busy not released after the dead-gateway turns"
    print(
        f"OK: a turn on a killed gateway was abandoned {waited:.1f}s after the kill (stall "
        f"window 4 s); the message behind it failed {behind_after:.1f}s later"
    )
    if observed.get("notified"):
        print("     relayed to /notify during the run: "
              f"{[n.get('message', '')[:60] for n in observed['notified']]!r}")
    return None


async def _amain() -> int:
    hermes_bin = os.environ.get("HERMES_BIN", DEFAULT_HERMES_BIN)
    if not Path(hermes_bin).exists():
        print(f"FAIL: hermes binary not found at {hermes_bin}")
        return 1

    plugin_port = _free_port()
    cb_port = _free_port()
    llm_port = _free_port()

    # Stand up the stub LLM first — hermes needs it reachable to
    # complete the agent reply. Stays up for the entire test.
    print(f"OK: standing up stub LLM on http://127.0.0.1:{llm_port}/v1")
    observed: dict = {}
    llm_runner, llm_site = await _stub_llm_server(llm_port, observed)

    # Tmp HERMES_HOME with the plugin enabled and our stub LLM as the
    # custom-provider endpoint.
    tmp = Path(tempfile.mkdtemp(prefix="hermes-fullchain-"))
    hermes_home = tmp / ".hermes"
    hermes_home.mkdir()
    # start.sh's bridge (see the module docstring), applied to this home.
    if (INSTALL_HOME / "installs").is_dir():
        (hermes_home / "installs").symlink_to(INSTALL_HOME / "installs", target_is_directory=True)
        os.environ.setdefault("HERMES_RUNTIME_DIR", str(INSTALL_HOME / "tools"))
        os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
        print(f"OK: bridged {hermes_home}/installs -> {INSTALL_HOME / 'installs'}")
    probe = tmp / "probe.txt"
    probe.write_text("e2e probe\n")
    observed["probe"] = str(probe)
    # Turn-lease feed (runtime#417), installed the way start.sh installs it:
    # the template plugin under $HERMES_HOME/plugins and ONE activity-file path
    # shared by the gateway (env at spawn) and the runtime (this process).
    shutil.copytree(
        REPO / "hermes_plugins" / "molecule_turn_activity",
        hermes_home / "plugins" / "molecule_turn_activity",
    )
    lease_dir = tmp / "turn-lease"
    lease_dir.mkdir(mode=0o700)
    activity = lease_dir / "tool_activity"
    activity.write_text("")
    stale = time.time() - 60
    os.utime(activity, (stale, stale))
    os.environ["MOLECULE_TOOL_ACTIVITY_FILE"] = str(activity)
    os.environ["MOLECULE_TOOL_ACTIVITY_POLL_SECONDS"] = "0.2"
    # The gateway's "still working" notice, the executor's liveness signal
    # inside one long tool call. start.sh sets 60 s; 2 s keeps this run short.
    os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] = "2"
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  default: \"test-model\"\n"
        "  provider: \"custom\"\n"
        f"  base_url: \"http://127.0.0.1:{llm_port}/v1\"\n"
        "  api_key: \"sk-stub-fullchain\"\n"
        "  api_mode: \"chat_completions\"\n"
        # hermes >= 0.19: entry-point plugins are OPT-IN via
        # plugins.enabled; without it the molecule_a2a plugin silently
        # never loads and /a2a/health below fails — which is exactly the
        # first 2026-07-23 boot hang this harness now guards (start.sh
        # emits the same block).
        # start.sh turns tool search off for every workspace.
        "tools:\n"
        "  tool_search:\n"
        "    enabled: \"off\"\n"
        "plugins:\n"
        "  enabled:\n"
        "    - molecule_turn_activity\n"
        "    - molecule_a2a\n"
        "platforms:\n"
        "  molecule-a2a:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      host: \"127.0.0.1\"\n"
        f"      port: {plugin_port}\n"
        f"      callback_url: \"http://127.0.0.1:{cb_port}/a2a/reply\"\n"
    )
    (hermes_home / ".env").write_text("HERMES_CUSTOM_API_KEY=sk-stub-fullchain\n")

    log_file = open(tmp / "gateway.log", "w+", buffering=1)
    proc = subprocess.Popen(
        [hermes_bin, "gateway", "run"],
        env={**os.environ, "HOME": str(tmp), "HERMES_HOME": str(hermes_home)},
        stdout=log_file, stderr=subprocess.STDOUT,
        cwd=str(tmp),
    )
    print(f"OK: spawned hermes gateway (pid {proc.pid})")

    executor = None
    try:
        if not _wait_url(
            f"http://127.0.0.1:{plugin_port}/a2a/health", timeout_secs=60
        ):
            log_file.seek(0)
            print("FAIL: /a2a/health unreachable. Gateway log tail:")
            print(log_file.read()[-3000:])
            return 1
        print(f"OK: hermes plugin /a2a/health responds")

        # Stand up the real executor pointing at the real plugin.
        os.environ["MOLECULE_A2A_PLATFORM_ENABLED"] = "true"
        os.environ["MOLECULE_A2A_PLATFORM_PORT"] = str(plugin_port)
        os.environ["MOLECULE_A2A_CALLBACK_PORT"] = str(cb_port)
        # Stable workspace-scoped chat_id (review wf_3a7b849d #5): without this
        # executor._derive_chat_id falls through to str() of each context's
        # auto-vivified MagicMock, so the second message would land in a
        # DIFFERENT session and the "same-session" claim below would be vacuous.
        os.environ["WORKSPACE_ID"] = "ws-e2e-fullchain"

        import executor as executor_mod
        from executor import HermesAgentProxyExecutor
        from molecule_runtime.adapters.base import AdapterConfig

        # The runtime's heartbeat object, so the busy accounting is observable.
        heartbeat = SimpleNamespace(active_tasks=0, current_task="")
        cfg = AdapterConfig(model="test-model", system_prompt="be terse", heartbeat=heartbeat)
        tap = _LogTap()
        logging.getLogger("executor").addHandler(tap)
        logging.getLogger("executor").setLevel(logging.INFO)
        chips: List[tuple] = []

        async def _record_chip(name, summary=None, status="ok"):
            chips.append((name, summary, status))

        executor_mod.emit_tool_call = _record_chip
        executor = HermesAgentProxyExecutor(cfg)
        await executor.start()
        print(f"OK: executor reply server up on http://127.0.0.1:{cb_port}/a2a/reply")

        queue = _CapturingQueue()
        # Use an asyncio.wait_for with a generous timeout — hermes
        # needs to dispatch through the full pipeline, hit our stub
        # LLM, send back via plugin.
        await asyncio.wait_for(
            executor.execute(_ctx("hello fullchain"), queue), timeout=60
        )

        assert len(queue.events) == 1, (
            f"expected 1 event, got {len(queue.events)}: {queue.events!r}"
        )

        # SECOND message on the SAME (now-existing) session. With WORKSPACE_ID
        # set above, this reuses the workspace-scoped session key rather than
        # minting a fresh one, so it exercises the existing-session dispatch
        # path through the pinned stack — catching plumbing regressions
        # (plugins.enabled opt-in, the connect() ABI, callback wiring) on a
        # non-cold session.
        #
        # HONESTY NOTE (review wf_3a7b849d #5): this does NOT deterministically
        # exercise hermes's BUSY-path authz gate (_handle_active_session_busy_
        # message), which only fires while a turn is IN FLIGHT — the stub echo
        # LLM completes in milliseconds, so forcing that race reliably is not
        # possible here without a flaky timing hack. The actual contract we own
        # for the 2026-07-23 active-session drop — the adapter DECLARING
        # authorization_is_upstream — is pinned deterministically by the plugin
        # repo's test_authorization_is_upstream. This round-trip is the
        # same-session delivery guard; that test is the authz-flag guard.
        queue2 = _CapturingQueue()
        await asyncio.wait_for(
            executor.execute(
                _ctx("second message", task_id="task-fullchain-2"), queue2
            ),
            timeout=60,
        )
        assert len(queue2.events) == 1, (
            "second same-session message dropped through the pinned stack: "
            f"{queue2.events!r}"
        )
        def _reply_text(event):
            return "".join(p.text for p in event.parts if getattr(p, "text", None))

        first, second = _reply_text(queue.events[0]), _reply_text(queue2.events[0])
        assert "echo[" in first and "hello fullchain" in first, (
            f"first reply did not come from the stub LLM via the plugin: {first!r}"
        )
        assert second == "echo[second message]", (
            f"second reply did not come from the stub LLM via the plugin: {second!r}"
        )
        print("OK: second same-session message round-tripped through the pinned stack")
        text = repr(queue.events[0])
        # Our stub echoes "echo[<user>]" — the user message is the
        # prompt the executor forwarded. The hermes pipeline may
        # decorate it with system instructions, so we just check the
        # echo marker survived the round trip.
        # Reaching here proves the WIRE SHAPE works end-to-end:
        #
        #   executor.execute() → POST /a2a/inbound
        #     → hermes plugin → MessageEvent dispatch
        #       → hermes pipeline → custom LLM call
        #     → hermes plugin send() → POST executor /a2a/reply
        #   → execute() Future resolved → emit on event_queue
        #
        # The reply CONTENT depends on whether the stub LLM speaks
        # hermes's full multi-turn / tool-loop expectations. Our stub is
        # a 60-line echo server; it'll return an error if hermes does a
        # tool-call iteration the stub doesn't handle. That's OK — what
        # matters here is that hermes can route through the plugin all
        # the way back to the executor, which it just did.
        #
        # We assert for ANY non-empty reply, plus that we did NOT see
        # the KeyError signature this test was originally written to
        # catch (regression guard).
        assert text, f"empty reply from executor: {text!r}"
        assert "KeyError" not in text, (
            f"hermes pipeline KeyError regression — see PLATFORMS lookup "
            f"in tools_config.py. Reply: {text!r}"
        )
        if "echo" in text or "hello" in text:
            print(f"OK: stub LLM round-tripped (reply contains echo marker)")
        else:
            print(f"OK: wire shape validated (LLM-content depends on stub)")
        print(f"     event repr: {text[:200]}")
        print(f"OK: full chain returned text containing 'echo' / 'hello'")
        print(f"     event repr: {text[:200]}")

        # THIRD message: a turn that runs a real hermes tool must reach the
        # runtime's turn lease (runtime#417). The gateway's
        # molecule_turn_activity plugin touches MOLECULE_TOOL_ACTIVITY_FILE,
        # and the runtime's own liveness scope (the wrapper main.py puts on
        # every executor) feeds the lease from that file during the turn.
        from molecule_runtime import turn_lease
        from molecule_runtime.turn_lease_executor import wrap_executor

        lease = turn_lease.TurnLease(ttl_seconds=900)
        turn_lease.install(lease)
        before = activity.stat().st_mtime_ns
        queue3 = _CapturingQueue()
        await asyncio.wait_for(
            wrap_executor(executor).execute(
                _ctx(f"please {TOOL_MARKER} now", task_id="task-fullchain-3"), queue3
            ),
            timeout=90,
        )
        third = _reply_text(queue3.events[0]) if queue3.events else ""
        assert observed.get("called"), (
            "the gateway offered no read_file/terminal tool to call: "
            f"offered={observed.get('offered')!r}"
        )
        assert third == "tool-done", f"the tool turn did not finish through the stub: {third!r}"
        assert activity.stat().st_mtime_ns > before, (
            "hermes ran a tool but MOLECULE_TOOL_ACTIVITY_FILE was never touched: the "
            "molecule_turn_activity plugin did not load, or the path never reached the gateway"
        )
        assert lease.observed_activity, (
            "the activity file advanced but the runtime turn lease never fed from it"
        )
        print(
            f"OK: hermes tool call ({observed['called']}) touched {activity} and fed "
            "the runtime turn lease"
        )

        failure = await _turn_supervision_scenarios(
            executor, executor_mod, heartbeat, tap, chips, observed, proc,
            f"http://127.0.0.1:{llm_port}",
        )
        if failure:
            log_file.seek(0)
            print(f"FAIL: {failure}")
            print("Gateway log tail:")
            print(log_file.read()[-6000:])
            print("Executor log tail:")
            print("\n".join(tap.lines[-80:]))
            return 1

    finally:
        if executor is not None:
            await executor.stop()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log_file.close()
        await llm_site.stop()
        await llm_runner.cleanup()

    print()
    print("✓ Full-chain local E2E passed:")
    print("  executor.execute() → real hermes /a2a/inbound → hermes pipeline")
    print("  → stub LLM /v1/chat/completions → hermes plugin send()")
    print("  → executor reply server → execute() emits on event_queue")
    print("  + hermes tool call → molecule_turn_activity → runtime turn lease")
    print("  + turn supervision: busy for the whole turn, a 12 s tool call past an 8 s")
    print("    stall window, routine self-wakes dropped and delegation results queued,")
    print("    one message at a time per chat (#393) with commands answered mid-turn,")
    print("    the cap's /stop ending a real run, the reply deadline with a late relay,")
    print("    a silent turn settled, a killed gateway abandoned without stranding the")
    print("    message behind it")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
