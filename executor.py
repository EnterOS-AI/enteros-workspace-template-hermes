"""A2A → hermes-agent bridge with two transports.

Default transport (``MOLECULE_A2A_PLATFORM_ENABLED=false``)
==========================================================
POST to ``http://127.0.0.1:8642/v1/chat/completions`` synchronously,
parse the OpenAI-shaped response, emit. No session continuity but no
plugin dependency.

This is the safe default while the plugin install path inside the
deployed image is being debugged: the staging E2E for PR #32 surfaced
a workspace boot failure where ``hermes gateway run`` did not bind
``:8645`` inside the container (root cause TBD; local
``scripts/e2e_full_chain.py`` runs against my laptop venv where the
plugin was already installed manually, so it didn't catch the
deployment-shape divergence). Flip back to plugin path with
``MOLECULE_A2A_PLATFORM_ENABLED=true`` once the image-side install
is verified.

Plugin transport (``MOLECULE_A2A_PLATFORM_ENABLED=true``)
=========================================================
POST each A2A turn to the in-container hermes-agent platform plugin's
``/a2a/inbound`` endpoint. Hermes processes the message through its full
pipeline (sessions, skills, tools, hooks) and POSTs the agent's reply
back to a callback server we run inside this executor. A correlation
table maps the inbound ``message_id`` to an ``asyncio.Future`` that the
``execute()`` call awaits — so the A2A response is delivered as soon as
hermes calls ``send()``, not by polling. Earns single-session continuity
for peer agents.

Wire shape
==========
The plugin POSTs replies to:

    POST <callback_url>
    Content-Type: application/json

    {"chat_id": "...", "content": "...",
     "reply_to": "<inbound message_id>", "metadata": {...}}

We correlate by ``reply_to`` and resolve the matching pending Future.
``chat_id`` is intentionally *not* used for correlation: it's coarser
than message_id (multiple in-flight messages on the same chat would
race). A send without ``reply_to`` is mid-run output (interim text, the
gateway's "still working" notice) and counts as liveness only. ``reply_to``
stays right because hermes gets one message per chat at a time: one drained
from hermes' own queue is answered under another message's id (hermes#393).

A turn stays tracked, and the workspace busy, until its reply arrives or the
turn supervisor gives up on it (see TurnSupervisionPolicy).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import socket
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional, Set

import httpx

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import new_text_message

from molecule_runtime.adapters.base import AdapterConfig
from molecule_runtime.executor_helpers import (
    extract_attached_files,
    extract_message_text,
)
try:
    from molecule_runtime.attachment_vision import append_image_descriptions
except ModuleNotFoundError:  # pragma: no cover - older local runtime
    async def append_image_descriptions(text, files):
        return text
try:
    from molecule_runtime.mcp_tools import (
        handle_molecule_tool_call,
        openai_function_tools,
    )
    _MOLECULE_TOOLS_AVAILABLE = True
except ImportError:
    _MOLECULE_TOOLS_AVAILABLE = False

# ADR-004 shared tool-call display: the engine owns the single `emit_tool_call`
# primitive that POSTs the `agent_log` activity row core#2636 reconstructs into
# the live progress line + persistent ToolTraceChips. Every adapter calls it at
# its own tool site so the chip renders identically across runtimes (before this
# ONLY the claude-code template emitted these rows; hermes showed a bare spinner).
# Guarded like the mcp_tools import above so the executor still imports on an
# older base runtime that predates the primitive — the emit just becomes a no-op.
try:
    from molecule_runtime.tool_trace import emit_tool_call, summarize_tool
    _TOOL_TRACE_AVAILABLE = True
except ImportError:  # pragma: no cover - older base runtime without the primitive
    _TOOL_TRACE_AVAILABLE = False

    async def emit_tool_call(name, summary=None, status="ok"):  # type: ignore[misc]
        return None

    def summarize_tool(name, args=None):  # type: ignore[misc]
        return ""

logger = logging.getLogger(__name__)


# --- legacy chat-completions config -----------------------------------
_DEFAULT_BASE = "http://127.0.0.1:8642/v1"
_REQUEST_TIMEOUT = 600.0

# Only inject tools if the shared Molecule MCP contract is installed.
_ACTIVE_TOOLS: list[dict[str, Any]] = (
    openai_function_tools() if _MOLECULE_TOOLS_AVAILABLE else []
)
_MAX_TOOL_ROUNDS = 5  # prevent unbounded loops if the model keeps calling tools

# --- plugin-path config ----------------------------------------------
_DEFAULT_PLUGIN_HOST = "127.0.0.1"
_DEFAULT_PLUGIN_PORT = 8645
_DEFAULT_CALLBACK_HOST = "127.0.0.1"
_DEFAULT_CALLBACK_PORT = 8646
_DEFAULT_CALLBACK_PATH = "/a2a/reply"
SECRET_HEADER = "X-Molecule-A2A-Secret"

# --- Turn supervision --------------------------------------------------------
# A plugin turn ends when the gateway answers it, not on a clock. Every message
# stays tracked until its answer arrives, and the workspace reports busy for
# that long: the runtime's idle loop, the scheduler's watchdog and the platform
# all read that before they send another autonomous wake.
#
# One message per chat at the gateway. A message that waits in hermes' own
# queue is drained inside the run ahead of it and answered under the id of the
# message that opened that run (hermes#393), and /stop leaves queued plugin
# messages parked to run next. So later messages wait here, in arrival order,
# and each gets the gateway to itself.
#
# Three clocks bound a turn:
# * reply deadline: the caller is answered before its own request gives up.
#   The platform abandons an A2A request at 30 minutes and then queues it to be
#   delivered again, which would run the work twice; some callers give up
#   sooner (_SHORT_FUSE_SELF_SOURCE_TYPES, _CHANNEL_REPLY_DEADLINE_SECONDS). The
#   clock starts when execute() gets the message and covers every dispatch the
#   turn makes. At the deadline the caller gets an interim reply and the turn's
#   answer is delivered later, where its origin belongs (_deliver_late).
# * stall: no sign of life from the gateway for HERMES_TURN_STALL_SECONDS. A
#   live run is never that quiet: it sends interim text, a "still working"
#   notice every HERMES_AGENT_NOTIFY_INTERVAL (start.sh sets it), and every
#   tool call rewrites MOLECULE_TOOL_ACTIVITY_FILE. A turn that quiet was lost
#   with a crashed or wedged gateway.
# * cap: the running turn has gone on for HERMES_TURN_MAX_SECONDS (0 = no cap);
#   the executor sends hermes' /stop.
#
# A turn can also end in hermes with nothing sent under its id: hermes drops a
# final that is only a silence marker ([SILENT], NO_REPLY) on these internal
# turns, and a message that ran inside a run hermes started itself (a
# background job finishing) is answered under that run's anchor, which is none.
# Such finals arrive with no reply_to and are held for the running turn. Once
# the gateway has been quiet for HERMES_TURN_SETTLE_SECONDS while its health
# check still answers, no run is going (a live one sends "still working" every
# HERMES_AGENT_NOTIFY_INTERVAL), and the turn is settled with the last held
# final, or with _NO_RESPONSE.
#
# Stall and cap default to the runtime's turn-lease bounds
# (MOLECULE_TURN_LEASE_TTL_SECONDS / A2A_COMPLETION_IDLE_TIMEOUT_SECONDS, and
# MOLECULE_MAX_TURN_SECONDS), so a hermes turn is judged by the same numbers as
# a native one.
_DEFAULT_STALL_SECONDS = 900.0
_DEFAULT_MAX_TURN_SECONDS = 3600.0
_DEFAULT_REPLY_DEADLINE_SECONDS = 1500.0
_DEFAULT_SELF_REPLY_DEADLINE_SECONDS = 60.0
_DEFAULT_TURN_POLL_SECONDS = 1.0
_DEFAULT_CONTROL_REPLY_SECONDS = 120.0
_DEFAULT_QUEUE_MAX = 32
# hermes' own default for HERMES_AGENT_NOTIFY_INTERVAL (start.sh sets 60).
_HERMES_NOTIFY_INTERVAL_DEFAULT = 180.0
# A long turn with no tool calls reports "still working" at most this often:
# the platform's stall watchdog probes a busy workspace after 12 minutes
# without activity and restarts it 5 minutes later.
_PROGRESS_ROW_SECONDS = 240.0
# How long the POST that hands a message to the plugin may take. The plugin
# answers it before the run starts, so a slow answer is a busy gateway, not a
# refusal (see _post_inbound).
_INBOUND_POST_TIMEOUT = 10.0

# Which caller gets which reply deadline. The runtime's own wakes below give up
# within minutes and never show the reply text to anyone: the delegation
# harvester after 120 s, the idle digest after 300 s (its forwarder drops the
# interim text), the lifecycle prompt after 600 s (it retries on a timeout and
# discards the reply). They get the short self-turn deadline. Every other
# self-wake keeps the regular one: the platform's first-boot greeting shows the
# reply text and falls back on its own after 90 s, and the scheduler's trigger
# lane waits for the real answer.
_SHORT_FUSE_SELF_SOURCE_TYPES = frozenset({"self-harvester", "self-idle", "self-lifecycle"})
# A channel plugin's inbound turn (channel_sdk.send_channel_message) waits
# 600 s and is never replayed.
_CHANNEL_REPLY_DEADLINE_SECONDS = 540.0
# What a caller gets when hermes ends its turn without an answer under its id:
# the runtime's own empty-reply sentinel, which the idle digest never shows.
_NO_RESPONSE = "(no response generated)"
# A retried send of the same unanchored text arrives within seconds; the same
# text later is a new message.
_UNANCHORED_DEDUP_SECONDS = 120.0

# Fast-ack for a routine self-wake that arrives while a turn runs. The
# "[autonomous " prefix is the one the idle digest's reply forwarder suppresses,
# so the skip never becomes a chat bubble.
_SELF_PING_SKIPPED = (
    "[autonomous wake skipped — a turn is already in flight; this routine wake "
    "recurs next cycle]"
)
# The runtime's own backpressure reply (a2a_executor, inbox at capacity).
_QUEUE_FULL_REPLY = '{"status":"busy","retry":true}'

# The runtime's routine self-ping set (a2a_executor._ROUTINE_SELF_SOURCE_TYPES),
# used only when that module cannot be imported. tests/test_turn_supervision.py
# fails if the two drift.
_ROUTINE_SELF_SOURCE_TYPES_FALLBACK = frozenset({
    "self-cron",
    "self-harvester",
    "self-idle",
    "self-scheduler",
    "self-goal-nudge",
    "self-delegation-result",
    "self-lifecycle",
})
# A native turn picks delegation results up from DELEGATION_RESULTS_FILE, which
# is why the runtime may drop these wakes mid-turn. Hermes never reads that
# file: the wake is the only copy of the results, and the harvester does not
# resend one it got a 2xx for. They queue instead.
_UNDROPPABLE_SELF_SOURCE_TYPES = frozenset({"self-harvester", "self-delegation-result"})
_DELEGATION_RESULTS_PREFIX = "Delegation results are ready"

# Commands hermes answers inline even while a run is going. A person may send
# one mid-turn (to approve a blocked command, or to stop the turn), so these
# skip the queue. The first three end the running turn.
_INTERRUPTING_COMMANDS = frozenset({"/stop", "/new", "/reset"})
_INLINE_COMMANDS = _INTERRUPTING_COMMANDS | frozenset(
    {"/approve", "/deny", "/status", "/usage", "/help"}
)

# Inbound `metadata.source_type` values that mean "this delivery is an
# autonomous self-turn, not a human talking". The scheduler plugin stamps
# `self-scheduler` (its TRIGGER_DEFAULT_SOURCE_TYPE) on every trigger turn.
#
# This is an ALLOW-LIST, not a passthrough, and that is the security-relevant
# part: `context.message.metadata` is peer-supplied on the A2A path, so a peer
# workspace can put anything in it. Forwarding arbitrary strings would let a
# peer claim its message is unattended and, once the receiving end honours the
# marker, skip the human approval a peer message is supposed to require. Only
# markers we recognise cross the boundary; everything else — including a
# missing, malformed, or unknown value — resolves to "" and takes the existing
# interactive path unchanged.
_SCHEDULED_SOURCE_TYPES = frozenset({"self-scheduler"})


def _record_tool_activity() -> None:
    """Tier-C turn-lease liveness ping on each tool call (RC #203, template side).

    The base runtime EXPORTS ``MOLECULE_TOOL_ACTIVITY_FILE`` (a private, 0600,
    per-turn file) when the mailbox kernel is on, and its turn-lease watcher
    refreshes the lease whenever this file's mtime advances. A hermes turn that
    is churning long tool calls emits no native runtime event, so without this
    ping the lease can go stale and a live turn is mistaken for a stall — the
    coarse tier-D output-liveness fallback. Touching the file on every tool call
    mirrors how claude-code's native ``on_tool_start`` feeds the lease.

    No-op when ``MOLECULE_TOOL_ACTIVITY_FILE`` is unset (off-kernel / older base
    image) — additive and byte-identical off-kernel. Never raises: a liveness
    ping must not break a tool call.
    """
    path = os.environ.get("MOLECULE_TOOL_ACTIVITY_FILE", "").strip()
    if not path:
        return
    try:
        with open(path, "w") as fh:
            fh.write("1")
    except OSError:
        return
    try:
        # Keep the liveness file private even if we won a create-race with the
        # runtime's ensure_tool_activity_file(). Best-effort (no-op on Windows).
        os.chmod(path, 0o600)
    except OSError:
        pass


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")



_SESSIONS_DIR = os.path.join("/tmp/.hermes", "sessions")
_TOOL_TRACE_MAX_STEPS = 40
_TOOL_TRACE_INPUT_MAX = 200

# --- Context-overflow auto-heal (issue #370) -------------------------------
# When a hermes session grows past the model's context window and compaction
# cannot shrink it further, the turn returns one of the strings below and the
# session stays on disk. Every later turn resumes the same oversized
# transcript and dies identically — the workspace is dead until someone
# clears the session by hand.
#
# hermes DOES ship its own remedy: gateway/run.py `_handle_message_with_agent`
# skips transcript persistence on a context-overflow failure and calls
# `reset_session()` (upstream #9893/#10063/#35809). Measured on the live
# molecule-a2a lane, that path does not fire: workspace 90139d37 logged 50
# "Cannot compress further" turns between 2026-08-12 and 08-13 while its
# state.db recorded only 2 sessions with end_reason='session_reset', BOTH of
# them operator-typed /new. The session opened 12:33Z was still current at
# 20:30Z with its token count climbing ~1.7k per failed tick. So the heal
# below is OUR lane's, and it does not assume upstream's ever runs.
#
# The reset itself is delegated to hermes's own /new command rather than
# reaching into its storage: /new evicts the cached agent, tears down tool
# resources, interrupts in-flight delegations and rotates the session in one
# supported operation. `state.db` — not the .jsonl snapshots this module
# reads for tool traces — is the canonical transcript store, so deleting
# files here would heal nothing.
#
# Patterns are deliberately NARROW: each is a multi-word phrase hermes emits
# on a genuine overflow. A bare "token"/"exceeded" would also match rate-limit
# and auth errors, and discarding a healthy session on a 429 would lose real
# conversation history.
_CONTEXT_OVERFLOW_PATTERNS = (
    "cannot compress further",          # "…(182,430 tokens). Cannot compress further."
    "context length exceeded",          # agent.conversation_loop terminal branch
    "max compression attempts",         # compression-attempt ceiling branch
    "request payload too large",        # the 413 sibling of the same dead end
    # hermes >= v0.21.4 (upstream 23036e20a6) replaced the four literals above
    # with plain-language copy (agent/turn_failure_copy.py site_copy
    # 'context_overflow' / 'payload_too_large'); the old phrases now reach only
    # logs. Keep both generations.
    "couldn't shrink it enough automatically",  # site_copy context_overflow + payload_too_large
    # Suffix the gateway appends to EVERY compression_exhausted reply when it
    # auto-resets the session (gateway/run_turn.py; same text at 29112bef;
    # locales/en.yaml auto_reset_context_exhausted at v0.22.5).
    "exceeded the maximum context size",
    # The gateway's own fallback reply (locales/en.yaml context_overflow at
    # v0.22.5, a literal in gateway/run.py at f97608f1). It carries no
    # auto-reset suffix and the gateway resets nothing. It is NOT proof of an
    # overflow: the gateway also sends it for a bare "400" error, or a 400/500
    # exception, once a session has more than 50 history rows
    # (gateway/run_turn.py is_context_overflow_failure_result,
    # run_turn_persistence.py _hmwa_agent_error_reply), and almost every
    # platform session is that long. See _GATEWAY_OVERFLOW_FALLBACK below.
    "too long for me to read all at once",
)

# The part of that suffix that says the GATEWAY has already reset the session
# on a compression-exhausted turn. hermes v0.22 keeps the messages queued
# behind such a turn and runs them on the fresh session; a /new from us on top
# would clear that queue and time those turns out. So the heal replays without
# sending one when it sees this.
_GATEWAY_AUTO_RESET_MARKER = "exceeded the maximum context size"

# The suffix-less fallback above. Because it also stands for an ordinary
# provider 400/500 on a long session, the heal first replays the turn on the
# SAME session, and only resets when the replay gets the fallback again: a
# reset on a transient error would wipe a healthy conversation for nothing.
_GATEWAY_OVERFLOW_FALLBACK = "too long for me to read all at once"


def _gateway_already_reset(text: str | None) -> bool:
    """True when the overflow reply says the gateway reset the session itself."""
    return _GATEWAY_AUTO_RESET_MARKER in (text or "").lower()


def _is_ambiguous_overflow_fallback(text: str | None) -> bool:
    """True for the gateway's fallback reply when nothing else marks an overflow."""
    lowered = (text or "").lower()
    if _GATEWAY_OVERFLOW_FALLBACK not in lowered:
        return False
    return not any(
        p in lowered for p in _CONTEXT_OVERFLOW_PATTERNS if p != _GATEWAY_OVERFLOW_FALLBACK
    )


def _is_context_overflow(text: str | None) -> bool:
    """True when a hermes reply is the terminal context-overflow failure.

    Matched case-insensitively against the reply text, which is all the
    plugin callback delivers (`/a2a/reply` carries `content`, not the
    gateway's structured `compression_exhausted` flag).
    """
    if not text:
        return False
    lowered = text.lower()
    return any(p in lowered for p in _CONTEXT_OVERFLOW_PATTERNS)


def _session_jsonl_snapshot() -> dict[str, int]:
    """Byte-offset snapshot of every session .jsonl file, taken BEFORE a turn
    dispatches. The gateway owns session state; the executor only ever READS
    the delta after the reply arrives, so tool calls made during the turn can
    be surfaced as the canvas tool_trace chain (parity with claude-code)."""
    sizes: dict[str, int] = {}
    try:
        for name in os.listdir(_SESSIONS_DIR):
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(_SESSIONS_DIR, name)
            try:
                sizes[p] = os.path.getsize(p)
            except OSError:
                continue
    except OSError:
        pass
    return sizes


def _tool_trace_from_session_delta(before: dict[str, int]) -> list[dict[str, str]]:
    """Parse assistant tool_calls out of the session-file bytes written since
    ``before`` and map them to the platform tool_trace shape
    ([{"tool": name, "input": truncated-args}]). Best-effort: any parse
    problem yields a shorter (or empty) trace, never an exception."""
    trace: list[dict[str, str]] = []
    try:
        for name in os.listdir(_SESSIONS_DIR):
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(_SESSIONS_DIR, name)
            start = before.get(p, 0)
            try:
                if os.path.getsize(p) <= start:
                    continue
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(start)
                    delta = fh.read()
            except OSError:
                continue
            for line in delta.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("role") != "assistant":
                    continue
                for tc in rec.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    tool = str(fn.get("name") or "").strip()
                    if not tool:
                        continue
                    args = fn.get("arguments")
                    entry: dict[str, str] = {"tool": tool}
                    if args:
                        entry["input"] = str(args)[:_TOOL_TRACE_INPUT_MAX]
                    trace.append(entry)
                    if len(trace) >= _TOOL_TRACE_MAX_STEPS:
                        return trace
    except Exception:  # noqa: BLE001 — presentation only, never break a reply
        return trace
    return trace


def _reply_message_with_tool_trace(text: str, trace: list[dict[str, str]]):
    """new_text_message + metadata.tool_trace when the turn used tools. The
    platform extracts result.metadata.tool_trace from the serialized reply
    (a2a_proxy_helpers.extractToolTrace) and the canvas renders the chain."""
    msg = new_text_message(text)
    if trace:
        try:
            msg.metadata.update({"tool_trace": trace})
        except Exception:  # noqa: BLE001 — metadata is optional decoration
            pass
    return msg


# --- Scheduled session reset (issue #383) -----------------------------------
# hermes no longer rotates a gateway conversation on a timer. Upstream
# 1d5d0594 ("stop time-triggered conversation rotation", since v0.21.5 and
# still retired at v0.22.5, hermes_cli/session_reset_retirement.py) made
# `session_reset:` in config.yaml and SESSION_IDLE_MINUTES /
# SESSION_RESET_HOUR inert: a conversation now lasts until someone sends /new.
# On a scheduled workspace nobody does, so one session grows for days and the
# agent starts imitating its own compacted history (#383: "nothing to do" with
# zero tool calls after ~12 h of ticks, cured by a single /new). The same three
# knobs are applied here instead, by sending hermes's own /new ahead of the
# turn that crosses the boundary. Unset keeps today's behaviour: no resets.
_SESSION_RESET_MODES = frozenset({"none", "idle", "daily", "both"})
# hermes's former SessionResetPolicy defaults, so the knobs mean what they meant.
_DEFAULT_SESSION_RESET_HOUR = 4
_DEFAULT_SESSION_IDLE_MINUTES = 1440


def _int_env_or_warn(name: str, default: int, low: int, high: Optional[int]) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value: Optional[int] = int(raw)
    except ValueError:
        value = None
    if value is None or value < low or (high is not None and value > high):
        accepted = f"{low}-{high}" if high is not None else f">= {low}"
        logger.warning(
            "%s=%r is not an integer in %s; using the default %d instead",
            name, raw, accepted, default,
        )
        return default
    return value


def _last_daily_boundary(now: float, at_hour: int) -> float:
    """Epoch of the most recent ``at_hour:00`` (container local time) <= now."""
    local = datetime.fromtimestamp(now)
    boundary = local.replace(hour=at_hour, minute=0, second=0, microsecond=0)
    if boundary > local:
        boundary -= timedelta(days=1)
    return boundary.timestamp()


@dataclass(frozen=True)
class SessionResetPolicy:
    mode: str = "none"
    at_hour: int = _DEFAULT_SESSION_RESET_HOUR
    idle_minutes: int = _DEFAULT_SESSION_IDLE_MINUTES

    @classmethod
    def from_env(cls) -> "SessionResetPolicy":
        """Read HERMES_SESSION_RESET_MODE / _HOUR / HERMES_SESSION_IDLE_MINUTES.

        An unrecognised value is never fatal: it is logged at WARNING and the
        default is used, so a typo cannot stop the workspace from serving.
        """
        raw_mode = os.environ.get("HERMES_SESSION_RESET_MODE", "").strip()
        mode = raw_mode.lower() or "none"
        if mode not in _SESSION_RESET_MODES:
            logger.warning(
                "HERMES_SESSION_RESET_MODE=%r is not one of none|idle|daily|both; "
                "hermes sessions will NOT be reset",
                raw_mode,
            )
            mode = "none"
        policy = cls(
            mode=mode,
            at_hour=_int_env_or_warn(
                "HERMES_SESSION_RESET_HOUR", _DEFAULT_SESSION_RESET_HOUR, 0, 23
            ),
            idle_minutes=_int_env_or_warn(
                "HERMES_SESSION_IDLE_MINUTES", _DEFAULT_SESSION_IDLE_MINUTES, 1, None
            ),
        )
        if policy.mode != "none":
            logger.info(
                "hermes session reset policy: mode=%s at_hour=%d (container local "
                "time) idle_minutes=%d",
                policy.mode, policy.at_hour, policy.idle_minutes,
            )
        return policy

    def reset_reason(
        self, now: float, last_start: Optional[float], last_end: Optional[float]
    ) -> Optional[str]:
        """Why a turn starting at ``now`` needs a fresh session, or None.

        ``idle``: the chat's previous turn finished more than ``idle_minutes``
        ago. ``daily``: the previous turn started before the latest
        ``at_hour`` boundary. The first turn this process sees on a chat never
        resets: a container restart is not a boundary (hermes's own rule).
        """
        if self.mode == "none" or last_start is None:
            return None
        if (
            self.mode in ("idle", "both")
            and last_end is not None
            and now - last_end > self.idle_minutes * 60
        ):
            return "idle"
        if self.mode in ("daily", "both") and last_start < _last_daily_boundary(
            now, self.at_hour
        ):
            return "daily"
        return None


class _SessionResetClock:
    """Per-chat turn timestamps and in-flight counts for SessionResetPolicy."""

    def __init__(
        self, policy: SessionResetPolicy, clock: Callable[[], float] = time.time
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._started: Dict[str, float] = {}
        self._finished: Dict[str, float] = {}
        self._in_flight: Dict[str, int] = {}

    def begin(self, chat_id: str, busy: bool = False) -> Optional[str]:
        """Record a turn start; return the reset reason if one is due first.

        A turn that overlaps one already in flight on the chat, or that the
        caller says is not alone (``busy``: the executor still has a message
        running or waiting there), is never reset, since /new would interrupt or
        wait behind that message, and does not move the start clock, so the next
        turn that starts alone still sees the boundary. Synchronous on purpose:
        two deliveries racing on the event loop cannot both see the chat idle.
        """
        if busy or self._in_flight.get(chat_id):
            self._in_flight[chat_id] = self._in_flight.get(chat_id, 0) + 1
            return None
        now = self._clock()
        reason = self.policy.reset_reason(
            now, self._started.get(chat_id), self._finished.get(chat_id)
        )
        self._started[chat_id] = now
        self._in_flight[chat_id] = 1
        return reason

    def end(self, chat_id: str) -> None:
        self._finished[chat_id] = self._clock()
        remaining = self._in_flight.get(chat_id, 0) - 1
        if remaining > 0:
            self._in_flight[chat_id] = remaining
        else:
            self._in_flight.pop(chat_id, None)


# --- Turn supervision (see the constants block near the top) ----------------


def _float_env_or_warn(
    name: str, default: float, low: float, high: Optional[float]
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value: Optional[float] = float(raw)
    except ValueError:
        value = None
    if value is None or value != value or value < low or (high is not None and value > high):
        accepted = f"{low:g}-{high:g}" if high is not None else f">= {low:g}"
        logger.warning(
            "%s=%r is not a number in %s; using the default %g instead",
            name, raw, accepted, default,
        )
        return default
    return value


def _runtime_turn_defaults() -> tuple[float, float]:
    """(stall, cap) from the runtime's turn-lease resolution, so the knobs the
    runtime documents for native turns also bound hermes turns."""
    try:
        from molecule_runtime import turn_lease

        return (
            float(turn_lease.default_ttl_seconds()),
            float(turn_lease.default_absolute_cap_seconds()),
        )
    except Exception:  # noqa: BLE001 — an older runtime without the helpers
        return (_DEFAULT_STALL_SECONDS, _DEFAULT_MAX_TURN_SECONDS)


@dataclass(frozen=True)
class TurnSupervisionPolicy:
    stall_seconds: float = _DEFAULT_STALL_SECONDS
    max_seconds: float = _DEFAULT_MAX_TURN_SECONDS
    reply_deadline_seconds: float = _DEFAULT_REPLY_DEADLINE_SECONDS
    self_reply_deadline_seconds: float = _DEFAULT_SELF_REPLY_DEADLINE_SECONDS
    poll_seconds: float = _DEFAULT_TURN_POLL_SECONDS
    control_seconds: float = _DEFAULT_CONTROL_REPLY_SECONDS
    queue_max: int = _DEFAULT_QUEUE_MAX
    drop_self_pings: bool = True
    # 0 = never settle a quiet turn early; the stall window still applies.
    settle_seconds: float = 0.0

    @classmethod
    def from_env(cls) -> "TurnSupervisionPolicy":
        """Read the HERMES_TURN_* knobs. A bad value is logged and the default used."""
        stall, cap = _runtime_turn_defaults()
        # Two still-working notices missed in a row: a live run sends one every
        # interval, so no run is going. Off when the notice is off.
        notify = _float_env_or_warn(
            "HERMES_AGENT_NOTIFY_INTERVAL", _HERMES_NOTIFY_INTERVAL_DEFAULT, 0.0, None
        )
        settle = 2 * notify + 10 if notify > 0 else 0.0
        return cls(
            settle_seconds=_float_env_or_warn("HERMES_TURN_SETTLE_SECONDS", settle, 0.0, None),
            stall_seconds=_float_env_or_warn("HERMES_TURN_STALL_SECONDS", stall, 0.01, None),
            max_seconds=_float_env_or_warn("HERMES_TURN_MAX_SECONDS", cap, 0.0, None),
            reply_deadline_seconds=_float_env_or_warn(
                "HERMES_TURN_REPLY_DEADLINE_SECONDS", _DEFAULT_REPLY_DEADLINE_SECONDS, 0.0, None
            ),
            self_reply_deadline_seconds=_float_env_or_warn(
                "HERMES_SELF_TURN_REPLY_DEADLINE_SECONDS",
                _DEFAULT_SELF_REPLY_DEADLINE_SECONDS, 0.0, None,
            ),
            poll_seconds=_float_env_or_warn(
                "HERMES_TURN_POLL_SECONDS", _DEFAULT_TURN_POLL_SECONDS, 0.01, 60.0
            ),
            control_seconds=_float_env_or_warn(
                "HERMES_CONTROL_REPLY_SECONDS", _DEFAULT_CONTROL_REPLY_SECONDS, 0.01, None
            ),
            queue_max=_int_env_or_warn("HERMES_TURN_QUEUE_MAX", _DEFAULT_QUEUE_MAX, 1, None),
            drop_self_pings=_bool_env("HERMES_DROP_SELF_PINGS_WHEN_BUSY", True),
        )


class HermesTurnAbandoned(RuntimeError):
    """The executor stopped waiting for a turn; ``str()`` is the reply text."""


@dataclass(frozen=True)
class _Caller:
    """Who is waiting for the turn whose dispatches run in this task, and until when."""

    origin: str  # "user" | "self" | "peer" | "channel"
    source_type: str
    brief: str
    deadline: float  # seconds, 0 = none; named in the interim reply
    deadline_at: Optional[float]  # monotonic


# Set by execute() for the whole turn, so the turn's every dispatch (the turn,
# an overflow replay, the /new before a retry) shares one caller and one
# deadline. Unset in the supervisor, whose /stop belongs to no caller.
_TURN_CALLER: contextvars.ContextVar[Optional[_Caller]] = contextvars.ContextVar(
    "hermes_turn_caller", default=None
)

# How long a message's origin is remembered, so an answer that arrives long
# after its caller was answered still goes where it belongs. The cap bounds a
# running turn, so a day is ample.
_LATE_REPLY_TTL = 86400.0


@dataclass
class _Turn:
    """One message the executor is carrying to the gateway, until it settles."""

    message_id: str
    chat_id: str
    # "turn": waits for the chat's slot at the gateway. "command": an inline
    # hermes command sent mid-turn, which skips the queue. "control": /new or
    # /stop sent by the executor itself.
    kind: str
    origin: str  # "user" | "self" | "peer" | "channel" | "control"
    source_type: str
    brief: str
    registered_at: float
    command: str = ""
    # The caller's reply deadline (see _Caller).
    deadline: float = 0.0
    deadline_at: Optional[float] = None
    # For /stop, /new or /reset from a person: the turn that was running when
    # it went out, the only one its answer may end.
    target: str = ""
    # When it was posted to the gateway; the cap is measured from here.
    started_at: Optional[float] = None
    dispatched: bool = False
    # Holds one heartbeat active_tasks slot until released.
    counted: bool = False
    # No caller is waiting any more (answered at the reply deadline, or its
    # task was cancelled). The turn keeps its place; its answer goes to
    # _deliver_late and its driver cleans it up.
    detached: bool = False
    # The supervisor is ending it (stall or cap); /stop is on its way.
    stopping: bool = False
    progress: int = 0
    # Finals that arrived with no reply_to while this turn ran (see the
    # turn-supervision notes), oldest first.
    unanchored: List[str] = field(default_factory=list)
    # When the supervisor last asked the gateway whether a quiet turn is over.
    probed_at: float = 0.0


def _message_metadata(context: Any) -> Optional[dict]:
    """The inbound envelope metadata, message level first, then the request
    level the runtime's own self-wakes stamp (executor_helpers SSOT)."""
    try:
        from molecule_runtime.executor_helpers import get_message_metadata

        return get_message_metadata(context)
    except Exception:  # noqa: BLE001 — an older runtime without the helper
        metadata = getattr(getattr(context, "message", None), "metadata", None)
        if metadata is None:
            metadata = getattr(context, "metadata", None)
        return metadata if isinstance(metadata, dict) else None


def _message_source_type(context: Any) -> str:
    value = (_message_metadata(context) or {}).get("source_type")
    return value if isinstance(value, str) else ""


def _is_droppable_self_ping(context: Any, text: str) -> bool:
    """A self-wake that may be dropped while a turn runs: the runtime's own
    drop-not-queue rule (a2a_executor._is_routine_self_message) for cron, idle,
    scheduler and lifecycle wakes, minus delegation results (see
    _UNDROPPABLE_SELF_SOURCE_TYPES)."""
    source_type = _message_source_type(context)
    if source_type in _UNDROPPABLE_SELF_SOURCE_TYPES:
        return False
    if not source_type and (text or "").lstrip().startswith(_DELEGATION_RESULTS_PREFIX):
        return False
    try:
        from molecule_runtime.a2a_executor import _is_routine_self_message

        return bool(_is_routine_self_message(context, text))
    except Exception:  # noqa: BLE001
        return source_type in _ROUTINE_SELF_SOURCE_TYPES_FALLBACK


def _command_of(text: str) -> str:
    """The leading hermes command of a one-line message ("/stop"), or ""."""
    stripped = (text or "").strip()
    if not stripped.startswith("/") or "\n" in stripped:
        return ""
    return stripped.split(None, 1)[0].lower()


def _is_self_source(source_type: str) -> bool:
    try:
        from molecule_runtime.a2a_executor import _is_self_source_type

        return bool(_is_self_source_type(source_type or None))
    except Exception:  # noqa: BLE001
        return bool(source_type) and source_type.startswith("self-")


def _brief(text: str) -> str:
    try:
        from molecule_runtime.shared_runtime import brief_task

        return brief_task(text)
    except Exception:  # noqa: BLE001
        return " ".join((text or "").split())[:60]


async def _set_current_task(heartbeat: Any, task: str) -> None:
    """The runtime's active_tasks/current_task accounting (increment on a
    non-empty task, decrement on ""), plus its immediate heartbeat push."""
    try:
        from molecule_runtime.executor_helpers import set_current_task
    except Exception:  # noqa: BLE001
        return
    try:
        await set_current_task(heartbeat, task)
    except Exception:  # noqa: BLE001 — busy telemetry must never fail a turn
        logger.debug("set_current_task failed", exc_info=True)


def _touch_turn_lease() -> None:
    try:
        from molecule_runtime import turn_lease

        turn_lease.touch_current()
    except Exception:  # noqa: BLE001
        pass


def _redact(text: str) -> str:
    try:
        from molecule_runtime.secret_redactor import redact_secrets

        return redact_secrets(text)
    except Exception:  # noqa: BLE001
        return text


# Which argument names the chip previews for the tools hermes uses most.
_TOOL_PREVIEW_KEYS = (
    "command", "path", "file_path", "url", "query", "pattern", "goal", "code", "name",
)
_TOOL_PREVIEW_CHARS = 80


def _tool_chip_summary(tool: str, args_text: str) -> str:
    """One canvas chip line for a hermes tool call, e.g. ``🛠 terminal: ls -la``."""
    preview = ""
    try:
        args = json.loads(args_text) if args_text else None
    except ValueError:
        args = None
    if isinstance(args, dict):
        for key in _TOOL_PREVIEW_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                preview = value
                break
    elif isinstance(args, str):
        preview = args
    preview = " ".join(_redact(preview).split())
    if len(preview) > _TOOL_PREVIEW_CHARS:
        preview = preview[: _TOOL_PREVIEW_CHARS - 1] + "…"
    return f"🛠 {tool}: {preview}" if preview else f"🛠 {tool}"


def _read_tool_event(path: str) -> Optional[dict]:
    """The molecule_turn_activity record in the activity file, or None."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.loads(fh.read(8192))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("v") != 1:
        return None
    if not isinstance(record.get("seq"), int) or not isinstance(record.get("tool"), str):
        return None
    return record


class HermesAgentProxyExecutor(AgentExecutor):
    """Forwards every A2A turn to hermes-agent."""

    def __init__(self, config: AdapterConfig):
        self._config = config

        # Legacy transport state.
        self._base = os.environ.get("HERMES_API_BASE", _DEFAULT_BASE).rstrip("/")

        # Plugin transport state. The reply server only boots if the
        # plugin path is enabled; otherwise the executor degrades to
        # the legacy proxy below.
        # Default false until the image-side plugin install is verified
        # — see module docstring. Operators flip on per workspace via env.
        self._use_plugin = _bool_env("MOLECULE_A2A_PLATFORM_ENABLED", False)
        self._plugin_host = os.environ.get(
            "MOLECULE_A2A_PLATFORM_HOST", _DEFAULT_PLUGIN_HOST
        )
        self._plugin_port = int(
            os.environ.get("MOLECULE_A2A_PLATFORM_PORT", _DEFAULT_PLUGIN_PORT)
        )
        self._callback_host = os.environ.get(
            "MOLECULE_A2A_CALLBACK_HOST", _DEFAULT_CALLBACK_HOST
        )
        self._callback_port = int(
            os.environ.get("MOLECULE_A2A_CALLBACK_PORT", _DEFAULT_CALLBACK_PORT)
        )
        self._shared_secret = (
            os.environ.get("MOLECULE_A2A_PLATFORM_SHARED_SECRET", "") or ""
        )

        self._pending: Dict[str, asyncio.Future] = {}
        # (message_id, sha256(content)) -> monotonic ts of delivery. Lets
        # _handle_reply tell a RETRY duplicate (same id, same content —
        # drop) from a genuinely new late reply (same id, new content —
        # relay via /notify). See _handle_reply's orphan branch.
        self._delivered: Dict[tuple, float] = {}
        # message_id -> ("user"|"peer", monotonic ts). Recorded at execute()
        # registration so the orphan branch knows the turn's ORIGIN: only
        # human canvas turns may relay to /notify — a late reply to a
        # PEER-delegated turn on the user's canvas is an out-of-context
        # bubble addressed to another agent (review wf_7cb5003d #5).
        self._origin: Dict[str, tuple] = {}
        self._reply_runner: Optional["web.AppRunner"] = None
        self._reply_site: Optional["web.TCPSite"] = None
        self._started: bool = False

        # Turn supervision (see TurnSupervisionPolicy).
        self._heartbeat = getattr(config, "heartbeat", None)
        self._turn_policy = TurnSupervisionPolicy.from_env()
        # Every message the executor is carrying, until it settles.
        self._turns: Dict[str, _Turn] = {}
        # chat_id -> message_id holding the chat's one slot at the gateway, the
        # ids waiting for it in arrival order, and the future each waiter is
        # parked on.
        self._slot: Dict[str, str] = {}
        self._queue: Dict[str, Deque[str]] = {}
        self._slot_waiters: Dict[str, asyncio.Future] = {}
        # chat_id -> monotonic time of the gateway's last send for that chat.
        self._chat_activity: Dict[str, float] = {}
        # chat_id -> turns holding a busy slot.
        self._busy_chats: Dict[str, int] = {}
        self._supervisor: Optional[asyncio.Task] = None
        self._stopping = False
        self._background: Set[asyncio.Task] = set()
        # (boot, seq) of the last tool event reported from the activity file.
        self._tool_event_mark: Optional[tuple] = None
        self._tool_file_mtime: float = 0.0
        self._tool_activity_at: float = 0.0
        # When the last agent_log row (tool chip or "still working") went out.
        self._activity_row_at: float = 0.0

        # Scheduled session reset (issue #383). None = never reset (default).
        reset_policy = SessionResetPolicy.from_env()
        self._session_clock: Optional[_SessionResetClock] = (
            _SessionResetClock(reset_policy) if reset_policy.mode != "none" else None
        )

    # ------------------------------------------------------------------
    # Lifecycle (called by adapter.create_executor)
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self._use_plugin:
            logger.info(
                "Hermes plugin path disabled; using /v1/chat/completions"
            )
            return
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError(
                "MOLECULE_A2A_PLATFORM_ENABLED=true but aiohttp is not "
                "installed — add aiohttp to requirements.txt or set "
                "MOLECULE_A2A_PLATFORM_ENABLED=false to use the legacy path."
            )
        await self._start_reply_server()
        policy = self._turn_policy
        logger.info(
            "hermes turn supervision: stall=%gs cap=%s reply_deadline=%s "
            "self_reply_deadline=%s settle=%s queue_max=%d poll=%gs "
            "drop_self_pings_when_busy=%s",
            policy.stall_seconds,
            f"{policy.max_seconds:g}s" if policy.max_seconds else "off",
            f"{policy.reply_deadline_seconds:g}s" if policy.reply_deadline_seconds else "off",
            f"{policy.self_reply_deadline_seconds:g}s" if policy.self_reply_deadline_seconds else "off",
            f"{policy.settle_seconds:g}s" if policy.settle_seconds else "off",
            policy.queue_max,
            policy.poll_seconds,
            policy.drop_self_pings,
        )
        notify = _float_env_or_warn(
            "HERMES_AGENT_NOTIFY_INTERVAL", _HERMES_NOTIFY_INTERVAL_DEFAULT, 0.0, None
        )
        if notify <= 0 or notify * 3 > policy.stall_seconds:
            logger.warning(
                "HERMES_AGENT_NOTIFY_INTERVAL=%g leaves the gateway's still-working "
                "notice too sparse (or off) for HERMES_TURN_STALL_SECONDS=%g: a "
                "turn that spends that long in one tool call may be abandoned",
                notify, policy.stall_seconds,
            )
        if policy.settle_seconds and (notify <= 0 or policy.settle_seconds < 2 * notify):
            logger.warning(
                "HERMES_TURN_SETTLE_SECONDS=%g is shorter than two still-working "
                "notices (HERMES_AGENT_NOTIFY_INTERVAL=%g): a live turn may be settled "
                "with no response",
                policy.settle_seconds, notify,
            )
        self._ensure_supervisor()

    async def stop(self) -> None:
        self._stopping = True
        # Busy slots are given back below, in one bounded step; the cleanup the
        # cancelled tasks run must not try to push them as well.
        counted = [t for t in self._turns.values() if t.counted]
        for turn in counted:
            turn.counted = False

        # Fail every pending future so in-flight execute() calls surface a
        # clear shutdown error instead of waiting on a reply that cannot come.
        # Nobody waits on a detached turn's future; cancelling it keeps asyncio
        # from reporting an exception that was never retrieved.
        for message_id, fut in list(self._pending.items()):
            if fut.done():
                continue
            turn = self._turns.get(message_id)
            if turn is not None and turn.detached:
                fut.cancel()
            else:
                fut.set_exception(RuntimeError("executor shutting down"))

        supervisor, self._supervisor = self._supervisor, None
        tasks = [t for t in [supervisor, *self._background] if t is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001 — cancelled or already failed
                pass
        self._background.clear()

        if self._reply_site is not None:
            try:
                await self._reply_site.stop()
            except Exception:
                logger.exception("hermes plugin: reply site stop failed")
            self._reply_site = None
        if self._reply_runner is not None:
            try:
                await self._reply_runner.cleanup()
            except Exception:
                logger.exception("hermes plugin: reply runner cleanup failed")
            self._reply_runner = None

        self._pending.clear()
        chats = {t.chat_id for t in self._turns.values()}
        self._turns.clear()
        self._slot.clear()
        self._queue.clear()
        self._slot_waiters.clear()
        self._busy_chats.clear()
        try:
            from molecule_runtime.runtime_inbox import get_inbox

            for chat_id in chats:
                entry = get_inbox().peek(f"hermes:{chat_id}")
                if entry is not None:
                    entry.turn_in_flight = False
                    entry.in_flight_source_type = None
        except Exception:  # noqa: BLE001
            pass
        # set_current_task drops active_tasks before it awaits its heartbeat
        # push, so even a push that times out here leaves the count right.
        if counted:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(_set_current_task(self._heartbeat, "") for _ in counted)),
                    2.0,
                )
            except Exception:  # noqa: BLE001
                pass

    async def _start_reply_server(self) -> None:
        app = web.Application()
        app.router.add_post(_DEFAULT_CALLBACK_PATH, self._handle_reply)

        self._reply_runner = web.AppRunner(app)
        await self._reply_runner.setup()
        self._reply_site = web.TCPSite(
            self._reply_runner, self._callback_host, self._callback_port
        )
        await self._reply_site.start()
        logger.info(
            "hermes plugin reply server listening on http://%s:%d%s",
            self._callback_host, self._callback_port, _DEFAULT_CALLBACK_PATH,
        )

    # ------------------------------------------------------------------
    # AgentExecutor contract
    # ------------------------------------------------------------------
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = extract_message_text(context.message) or ""
        # Phase 1 file-only message support (a1ea2200 archaeology — chloe-dong
        # PDF-only canary 2026-05-20 01:04:27Z surfaced the opaque
        # "(empty prompt — nothing to do)" reply). Mirror the claude-code
        # reference impl (claude_sdk_executor.py:644-650): surface attached
        # files to hermes as a manifest in the prompt — hermes reads files
        # through its own tools by path. Phase 2 will wire actual
        # file-content forwarding to the hermes daemon.
        attached = extract_attached_files(context.message)
        if attached:
            text = await append_image_descriptions(text, attached)
            manifest = "\n\nAttached files:\n" + "\n".join(
                f"- {f['name']} ({f['mime_type'] or 'unknown type'}) at {f['path']}"
                for f in attached
            )
            text = (text + manifest) if text.strip() else manifest.lstrip()
        if not text.strip():
            # Truly empty — actionable per
            # feedback_surface_actionable_failure_reason_to_user.
            await event_queue.enqueue_event(
                new_text_message(
                    "Your message was empty. Please send text or a file "
                    "with instructions."
                )
            )
            return
        prompt = text

        if self._use_plugin:
            # A routine self-wake (idle digest, cron, scheduler, harvester)
            # that lands while a turn runs is dropped, as the runtime's native
            # executor drops it: it recurs next cycle, and forwarding it would
            # queue it in the gateway behind the running turn, where a backlog
            # of stale wakes runs for hours once the turn ends.
            if self._should_skip_self_ping(context, prompt):
                await event_queue.enqueue_event(new_text_message(_SELF_PING_SKIPPED))
                return
            # Plugin path → hermes daemon owns session state natively
            # (gateway/session.py SessionStore: SQLite + JSONL, keyed by
            # session_key derived from SessionSource.chat_id).
            #
            # PR #34 dropped messages_history from the payload trusting
            # daemon persistence. Empirically (chloe-dong workspace
            # 5192737f, 2026-05-20) that's insufficient: HERMES_HOME
            # defaults to container-/tmp which is volatile across
            # workspace restarts, and a fresh daemon has no transcript
            # to replay. Belt-and-suspenders: re-attach canvas-side
            # history so the plugin adapter can seed the daemon's
            # transcript on a fresh session. Once daemon persistence is
            # durable (separate task: move HERMES_HOME to a workspace
            # volume) this can be revisited. See RFC #600 sibling
            # discussion in #497.
            history = self._extract_history_from_context(context)
            # The caller's deadline runs from here and covers the scheduled /new.
            token = _TURN_CALLER.set(self._caller_of(context, prompt))
            try:
                clock = getattr(self, "_session_clock", None)
                if clock is None:
                    await self._execute_via_plugin(
                        context, event_queue, prompt, history=history
                    )
                    return
                chat_id = self._derive_chat_id(context)
                # While another message runs or waits on the chat, /new would
                # wait behind it; the reset waits for a turn that starts alone.
                reason = clock.begin(chat_id, busy=self._chat_busy(chat_id))
                try:
                    if reason:
                        await self._scheduled_session_reset(context, chat_id, reason)
                    await self._execute_via_plugin(
                        context, event_queue, prompt, history=history
                    )
                finally:
                    clock.end(chat_id)
            finally:
                _TURN_CALLER.reset(token)
        else:
            # Legacy chat_completions path: /v1/chat/completions is
            # stateless by contract — it has no session store of its own,
            # so the platform MUST thread context. Keep history extraction
            # for this path until/unless this fallback is retired.
            history = self._extract_history_from_context(context)
            await self._execute_via_chat_completions(
                event_queue, prompt, history=history
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The chat-completions path is bounded by its HTTP timeout and the
        # plugin path by the turn supervisor (stall and cap; the cap sends
        # hermes' /stop). Neither offers a per-request cancel.
        return None

    # ------------------------------------------------------------------
    # Plugin transport
    # ------------------------------------------------------------------
    async def _execute_via_plugin(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        # Every dispatch this turn makes (the turn, an overflow replay, the
        # retry) shares the turn's caller; _dispatch_and_wait reads it from the
        # context so its signature stays what callers and tests know.
        if _TURN_CALLER.get() is not None:
            await self._run_plugin_turn(context, event_queue, prompt, history)
            return
        token = _TURN_CALLER.set(self._caller_of(context, prompt))
        try:
            await self._run_plugin_turn(context, event_queue, prompt, history)
        finally:
            _TURN_CALLER.reset(token)

    def _caller_of(self, context: RequestContext, prompt: str) -> _Caller:
        """Who sent this message, and when its reply is due."""
        raw_source_type = _message_source_type(context)
        peer_id, _ = self._derive_peer_identity(context)
        message_meta = getattr(getattr(context, "message", None), "metadata", None)
        request_meta = getattr(context, "metadata", None)
        if peer_id or (isinstance(message_meta, dict) and message_meta.get("delegation_id")):
            # The platform's async delegation names its delegation_id; it stores
            # the reply as the delegator's result.
            origin = "peer"
        elif _is_self_source(raw_source_type):
            origin = "self"
        elif (
            isinstance(request_meta, dict)
            and isinstance(request_meta.get("source"), str)
            and request_meta["source"].strip()
        ):
            # The runtime stamps a channel plugin's id there (channel_events).
            origin = "channel"
        else:
            origin = "user"
        policy = getattr(self, "_turn_policy", None) or TurnSupervisionPolicy()
        if origin == "self" and raw_source_type in _SHORT_FUSE_SELF_SOURCE_TYPES:
            deadline = policy.self_reply_deadline_seconds
        elif origin == "channel" and policy.reply_deadline_seconds:
            deadline = min(policy.reply_deadline_seconds, _CHANNEL_REPLY_DEADLINE_SECONDS)
        else:
            deadline = policy.reply_deadline_seconds
        return _Caller(
            origin=origin,
            source_type=raw_source_type,
            brief=_brief(prompt),
            deadline=deadline,
            deadline_at=time.monotonic() + deadline if deadline else None,
        )

    async def _run_plugin_turn(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        prompt: str,
        history: list[dict[str, Any]] | None,
    ) -> None:
        chat_id = self._derive_chat_id(context)
        peer_id, peer_name = self._derive_peer_identity(context)
        source_type = self._derive_source_type(context)
        callback_url = (
            f"http://{self._callback_host}:{self._callback_port}{_DEFAULT_CALLBACK_PATH}"
        )

        # Tool-trace capture (canvas chain parity with claude-code): snapshot
        # the session files before dispatch; the post-reply delta holds the
        # turn's assistant tool_calls.
        session_snapshot = _session_jsonl_snapshot()

        text = await self._dispatch_and_wait(
            content=prompt,
            chat_id=chat_id,
            peer_id=peer_id,
            peer_name=peer_name,
            callback_url=callback_url,
            history=history,
            event_queue=event_queue,
            source_type=source_type,
        )
        if text is None:
            return  # the failure message was already enqueued

        # --- Context-overflow auto-heal (issue #370) ---
        # The session is permanently too large to process; resuming it makes
        # every future turn fail identically. Clear it via hermes's own /new
        # and replay this turn ONCE on the fresh session.
        #
        # Bounded to a single attempt: a second overflow on an empty session
        # means THIS prompt is itself oversized, which no reset can fix, so
        # the overflow text is delivered rather than looping.
        #
        # The gateway's suffix-less fallback is ambiguous (a provider 400/500
        # on any long session gets the same text), so it first earns one
        # replay on the SAME session; only a second fallback goes on to the
        # reset below. Worst case: turn, replay, /new, replay.
        if _is_ambiguous_overflow_fallback(text):
            logger.warning(
                "auto-heal: hermes overflow fallback on chat_id=%s — replaying "
                "once on the same session before any reset. reply=%s",
                chat_id,
                text[:200],
            )
            text = await self._dispatch_and_wait(
                content=prompt,
                chat_id=chat_id,
                peer_id=peer_id,
                peer_name=peer_name,
                callback_url=callback_url,
                history=history,
                event_queue=event_queue,
                source_type=source_type,
            )
            if text is None:
                return
        if _is_context_overflow(text):
            already_reset = _gateway_already_reset(text)
            logger.error(
                "auto-heal: hermes context overflow on chat_id=%s — %s and "
                "retrying once. reply=%s",
                chat_id,
                "the gateway already reset the session" if already_reset
                else "resetting the session",
                text[:200],
            )
            if already_reset or await self._reset_hermes_session(
                chat_id=chat_id,
                peer_id=peer_id,
                peer_name=peer_name,
                callback_url=callback_url,
            ):
                # Re-seed from the caller-supplied history: the fresh session
                # has no transcript, so without this the retry starts amnesiac.
                retry = await self._dispatch_and_wait(
                    content=prompt,
                    chat_id=chat_id,
                    peer_id=peer_id,
                    peer_name=peer_name,
                    callback_url=callback_url,
                    history=history,
                    event_queue=event_queue,
                    # The retry IS the same turn — it must not change
                    # provenance, or a scheduled turn would become
                    # interactive purely by having overflowed once.
                    source_type=source_type,
                )
                if retry is None:
                    return
                if _is_context_overflow(retry):
                    logger.error(
                        "auto-heal: still overflowing on a FRESH session for "
                        "chat_id=%s — the prompt itself exceeds the window",
                        chat_id,
                    )
                text = retry

        await event_queue.enqueue_event(
            _reply_message_with_tool_trace(
                text, _tool_trace_from_session_delta(session_snapshot)
            )
        )

    async def _scheduled_session_reset(
        self, context: RequestContext, chat_id: str, reason: str
    ) -> None:
        """Put this turn on a fresh hermes session (see SessionResetPolicy).

        A failed reset is logged by ``_reset_hermes_session`` and the turn
        proceeds on the existing session: a missed reset must not cost the turn.
        """
        peer_id, peer_name = self._derive_peer_identity(context)
        logger.info(
            "session reset policy: %s boundary passed for chat_id=%s; sending /new "
            "before this turn",
            reason,
            chat_id,
        )
        await self._reset_hermes_session(
            chat_id=chat_id,
            peer_id=peer_id,
            peer_name=peer_name,
            callback_url=(
                f"http://{self._callback_host}:{self._callback_port}"
                f"{_DEFAULT_CALLBACK_PATH}"
            ),
        )

    async def _reset_hermes_session(
        self,
        *,
        chat_id: str,
        peer_id: str,
        peer_name: str | None,
        callback_url: str,
    ) -> bool:
        """Clear this chat's hermes session by sending the `/new` command.

        `/new` is hermes's own supported reset: it evicts the cached agent,
        tears down tool resources, interrupts in-flight delegations and
        rotates the session id. It is gated behind an interactive confirm
        unless `approvals.destructive_slash_confirm` is false — start.sh
        writes that key for exactly this reason (an unattended workspace has
        nobody to confirm). Returns True when the reset reply came back.
        """
        reply = await self._dispatch_and_wait(
            content="/new",
            chat_id=chat_id,
            peer_id=peer_id,
            peer_name=peer_name,
            callback_url=callback_url,
            history=None,
            event_queue=None,
        )
        if reply is None:
            logger.error("auto-heal: /new dispatch failed for chat_id=%s", chat_id)
            return False
        # A confirm prompt instead of a reset means the approvals gate is
        # still on: the session was NOT cleared, so retrying would replay the
        # same oversized transcript. Say so loudly rather than silently
        # burning the retry.
        if _is_context_overflow(reply):
            logger.error(
                "auto-heal: /new itself returned an overflow reply for "
                "chat_id=%s — session not cleared",
                chat_id,
            )
            return False
        logger.info(
            "auto-heal: hermes session reset for chat_id=%s (reply=%s)",
            chat_id,
            reply[:120],
        )
        return True

    async def _dispatch_and_wait(
        self,
        *,
        content: str,
        chat_id: str,
        peer_id: str,
        peer_name: str | None,
        callback_url: str,
        history: list[dict[str, Any]] | None,
        event_queue: EventQueue | None,
        source_type: str = "",
    ) -> str | None:
        """Give one message to the hermes plugin and wait for its answer.

        Returns the reply text, or None when the turn failed or its caller was
        answered at the reply deadline; the text for the caller is then already
        enqueued. Dispatches with no ``event_queue`` are control messages (the
        `/new` reset, the supervisor's `/stop`): silent, and bounded by
        HERMES_CONTROL_REPLY_SECONDS, and by the deadline of the turn they are
        sent for.
        """
        message_id = uuid.uuid4().hex
        control = event_queue is None
        caller = _TURN_CALLER.get()
        deadline, deadline_at = (caller.deadline, caller.deadline_at) if caller else (0.0, None)
        target = ""
        if control:
            kind, origin, raw_source_type, brief = "control", "control", "", content
        else:
            if caller is not None:
                origin, raw_source_type, brief = caller.origin, caller.source_type, caller.brief
            else:
                # A dispatch outside execute(): its own caller, its own clock.
                origin = "peer" if peer_id else "user"
                raw_source_type, brief = source_type, _brief(content)
                deadline = self._turn_policy.reply_deadline_seconds
                deadline_at = time.monotonic() + deadline if deadline else None
            command = _command_of(content)
            if (
                origin in ("user", "channel")
                and command in _INLINE_COMMANDS
                and self._chat_busy(chat_id)
            ):
                kind = "command"
                if command in _INTERRUPTING_COMMANDS:
                    running = self._running_turn(chat_id)
                    target = running.message_id if running is not None else ""
            else:
                kind = "turn"
            if kind == "turn" and len(self._queue.get(chat_id, ())) >= self._turn_policy.queue_max:
                logger.warning(
                    "hermes plugin: %d messages already waiting on %s; refusing another",
                    len(self._queue.get(chat_id, ())), chat_id,
                )
                await event_queue.enqueue_event(new_text_message(_QUEUE_FULL_REPLY))
                return None

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[message_id] = future
        self._record_origin(message_id, origin, raw_source_type)

        # Belt-and-suspenders session continuity (task #385): the hermes
        # daemon owns session state natively, but its store at
        # HERMES_HOME=/tmp/.hermes is container-volatile, so a fresh
        # daemon comes up with no transcript to replay. Canvas already
        # ships the last 20 turns via metadata.history (see
        # canvas/src/components/tabs/chat/hooks/useChatSend.ts) — we
        # forward those as messages_history so the plugin adapter can
        # seed the daemon's transcript on a fresh session (no-op when
        # the daemon already has the turns). Once daemon persistence is
        # durable this layer becomes a tautology and can be removed.
        #
        # peer_id/peer_name are the SENDER's identity, NOT a copy of
        # chat_id. Per molecule MCP protocol semantics: peer_id="" for
        # canvas_user, set to peer workspace UUID for peer_agent. PR #34
        # (task #262) decoupled them — keep that fix intact.
        payload = {
            "chat_id": chat_id,
            "peer_id": peer_id,
            "peer_name": peer_name,
            "content": content,
            "message_id": message_id,
            "callback_url": callback_url,
        }
        # Autonomy marker, forwarded ONLY for a recognised scheduled delivery
        # (see _SCHEDULED_SOURCE_TYPES). Absent for every interactive turn, so
        # an interactive payload is byte-identical to what it was before — a
        # human message keeps prompting a human, which is the point.
        #
        # STATED PLAINLY: this is currently INERT end-to-end, and shipping it
        # is a deliberate half-fix, not a dead knob left by accident. The
        # plugin passes the payload through as MessageEvent.raw_message, so
        # the marker does reach hermes — but the hermes we pin resolves cron
        # provenance in gateway/run.py::_set_session_env, which is a single
        # `return set_session_vars(..., cron_session="")` with no branch and
        # no field on SessionContext.source for a source_type to ride in.
        # Verified byte-identical at upstream HEAD 63279301, so the pin bump
        # in this same change does NOT close it either. Forwarding the marker
        # is the half we own; the other half needs an upstream change. Until
        # that lands, a scheduled run whose approval nobody is listening for
        # still blocks on the approval timeout.
        if source_type:
            payload["source_type"] = source_type
        # Defensive: if history is malformed (not a list), log + skip
        # rather than crash the turn. Per
        # feedback_surface_actionable_failure_reason_to_user, we still
        # deliver the user's message — they get a stateless reply
        # instead of an opaque error.
        if history is not None:
            if isinstance(history, list) and history:
                payload["messages_history"] = history
            elif not isinstance(history, list):
                logger.warning(
                    "hermes plugin: messages_history is not a list "
                    "(type=%s); skipping seed",
                    type(history).__name__,
                )
        headers = {"Content-Type": "application/json"}
        if self._shared_secret:
            headers[SECRET_HEADER] = self._shared_secret

        turn = _Turn(
            message_id=message_id,
            chat_id=chat_id,
            kind=kind,
            origin=origin,
            source_type=raw_source_type or "",
            brief=brief or "hermes turn",
            registered_at=time.monotonic(),
            command=_command_of(content) if kind == "command" else "",
            deadline=deadline,
            deadline_at=deadline_at,
            target=target,
        )
        self._turns[message_id] = turn
        if kind != "control":
            self._claim_busy(turn)
        if self._spawn(self._drive(turn, payload, headers, future)) is None:
            self._retire(turn)
            if event_queue is not None:
                await event_queue.enqueue_event(
                    new_text_message("[hermes plugin error] executor shutting down")
                )
            return None
        try:
            if kind == "control":
                return await self._await_control(turn, future)
            return await self._await_outcome(turn, future, event_queue)
        except asyncio.CancelledError:
            # The caller went away; the turn did not. It keeps its place (and,
            # if it is running, the gateway) and its answer is delivered late.
            if not future.done():
                turn.detached = True
                logger.warning(
                    "hermes plugin: caller of message_id=%s went away; the turn "
                    "keeps its place and its answer will be delivered late",
                    message_id,
                )
            raise
        finally:
            if not turn.detached:
                self._retire(turn)

    async def _drive(
        self, turn: _Turn, payload: Dict[str, Any], headers: Dict[str, str],
        future: asyncio.Future,
    ) -> None:
        """Take the chat's slot, post the message, and hold the slot until the
        message settles. Its own task, so a caller that stops waiting (the reply
        deadline, a cancellation) does not take the turn down with it."""
        holds_slot = turn.kind == "turn" or (
            turn.kind == "control" and payload.get("content") != "/stop"
        )
        try:
            if holds_slot:
                await self._acquire_slot(turn.chat_id, turn.message_id)
            if future.done():
                return
            turn.started_at = time.monotonic()
            error = await self._post_inbound(payload, headers)
            if error is not None:
                self._fail(turn, future, f"[hermes plugin POST error] {error}")
                return
            turn.dispatched = True
            if turn.kind == "turn":
                # Bounded by the supervisor: stall and cap.
                await asyncio.wait({future})
            else:
                await asyncio.wait({future}, timeout=self._turn_policy.control_seconds)
                self._fail(
                    turn, future,
                    f"[hermes plugin: no reply to {payload.get('content', '')[:20]!r} "
                    f"within {self._turn_policy.control_seconds:g}s]",
                )
        finally:
            if holds_slot:
                self._release_slot(turn.chat_id, turn.message_id)
            if turn.unanchored:
                # Held for this turn, never its answer: other runs' output.
                held, turn.unanchored = turn.unanchored, []
                self._spawn(self._relay_held(turn.chat_id, held))
            if turn.detached:
                self._retire(turn)

    async def _post_inbound(self, payload: Dict[str, Any], headers: Dict[str, str]) -> Optional[str]:
        """POST one message to the plugin's /a2a/inbound; the error text, or None.

        A request that was sent but not answered in time counts as delivered:
        the plugin may already have the message, and treating it as refused
        would let the next one land behind it. The stall window catches a
        gateway that is really gone.
        """
        inbound_url = f"http://{self._plugin_host}:{self._plugin_port}/a2a/inbound"
        try:
            async with httpx.AsyncClient(timeout=_INBOUND_POST_TIMEOUT) as client:
                resp = await client.post(inbound_url, json=payload, headers=headers)
                resp.raise_for_status()
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            logger.warning(
                "hermes plugin: no answer to the POST of message_id=%s within %gs (%s); "
                "treating it as delivered",
                payload.get("message_id"), _INBOUND_POST_TIMEOUT, type(exc).__name__,
            )
        except httpx.HTTPError as exc:
            logger.exception("hermes plugin POST failed")
            return str(exc)
        return None

    async def _acquire_slot(self, chat_id: str, message_id: str) -> None:
        if chat_id not in self._slot:
            self._slot[chat_id] = message_id
            return
        waiter = asyncio.get_running_loop().create_future()
        self._slot_waiters[message_id] = waiter
        self._queue.setdefault(chat_id, deque()).append(message_id)
        try:
            await waiter
        finally:
            self._slot_waiters.pop(message_id, None)
            queue = self._queue.get(chat_id)
            if queue is not None and message_id in queue:
                queue.remove(message_id)
                if not queue:
                    self._queue.pop(chat_id, None)

    def _release_slot(self, chat_id: str, message_id: str) -> None:
        """Hand the chat's slot to the next waiter, if ``message_id`` holds it."""
        if self._slot.get(chat_id) != message_id:
            return
        queue = self._queue.get(chat_id)
        while queue:
            nxt = queue.popleft()
            waiter = self._slot_waiters.get(nxt)
            if waiter is not None and not waiter.done():
                self._slot[chat_id] = nxt
                if not queue:
                    self._queue.pop(chat_id, None)
                waiter.set_result(None)
                return
        self._queue.pop(chat_id, None)
        self._slot.pop(chat_id, None)

    def _chat_busy(self, chat_id: str) -> bool:
        return chat_id in self._slot or bool(self._queue.get(chat_id))

    def _fail(self, turn: _Turn, future: asyncio.Future, text: str) -> None:
        """Settle an unanswered turn with ``text`` as its caller's reply."""
        if future.done():
            return
        if turn.detached:
            # Nobody will read it; a set exception would only be reported as
            # never retrieved.
            future.cancel()
        else:
            future.set_exception(HermesTurnAbandoned(text))

    def _retire(self, turn: _Turn) -> None:
        self._pending.pop(turn.message_id, None)
        self._turns.pop(turn.message_id, None)
        self._release_busy(turn)

    def _interim_reply(self, turn: _Turn, deadline: float) -> str:
        if turn.origin == "self":
            return (
                f"[autonomous turn still running after {deadline:g}s; it continues, "
                "and its outcome is reported when it ends]"
            )
        if turn.origin == "peer":
            return (
                f"[hermes is still working on this after {deadline:g}s; its answer "
                "cannot be returned to this request]"
            )
        if turn.origin == "channel":
            return (
                f"[hermes is still working on this after {deadline:g}s; its answer "
                "will be posted in the workspace's own chat, not here]"
            )
        return (
            f"[hermes is still working on this after {deadline:g}s; its answer will "
            "appear in this chat when it is ready]"
        )

    async def _await_outcome(
        self, turn: _Turn, future: asyncio.Future, event_queue: EventQueue
    ) -> str | None:
        """Wait for the turn's outcome, or answer the caller at the reply deadline.

        Waits in slices only to restart a supervisor that died, so a bug there
        cannot turn into an unbounded wait here.
        """
        slice_seconds = max(1.0, 5 * self._turn_policy.poll_seconds)
        while not future.done():
            timeout = slice_seconds
            if turn.deadline_at is not None:
                remaining = turn.deadline_at - time.monotonic()
                if remaining <= 0:
                    turn.detached = True
                    logger.info(
                        "hermes turn %s: caller answered at the %gs reply deadline; "
                        "the turn keeps running and its answer will be delivered late",
                        turn.message_id, turn.deadline,
                    )
                    await event_queue.enqueue_event(
                        new_text_message(self._interim_reply(turn, turn.deadline))
                    )
                    return None
                timeout = min(timeout, remaining)
            await asyncio.wait({future}, timeout=timeout)
            self._ensure_supervisor()
        if future.cancelled():
            await event_queue.enqueue_event(
                new_text_message("[hermes plugin error] the turn was cancelled")
            )
            return None
        try:
            text = future.result()
        except HermesTurnAbandoned as exc:
            await event_queue.enqueue_event(new_text_message(str(exc)))
            return None
        except Exception as exc:
            await event_queue.enqueue_event(new_text_message(f"[hermes plugin error] {exc!s}"))
            return None
        logger.info(
            "hermes turn %s answered after %.1fs (%d progress sends)",
            turn.message_id, time.monotonic() - turn.registered_at, turn.progress,
        )
        return text

    async def _await_control(self, turn: _Turn, future: asyncio.Future) -> str | None:
        # The driver bounds the wait once the message is out; the deadline of
        # the turn it was sent for also bounds the wait for the slot.
        if turn.deadline_at is None:
            await asyncio.wait({future})
        else:
            remaining = turn.deadline_at - time.monotonic()
            if remaining > 0:
                await asyncio.wait({future}, timeout=remaining)
            self._fail(
                turn, future,
                f"[hermes plugin: {turn.brief[:20]!r} not answered by the caller's "
                f"{turn.deadline:g}s reply deadline]",
            )
        if future.cancelled():
            return None
        try:
            return future.result()
        except Exception as exc:
            logger.error("hermes plugin: %r: %s", turn.brief[:20], exc)
            return None

    async def _handle_reply(self, request: "web.Request") -> "web.Response":
        if self._shared_secret:
            provided = request.headers.get(SECRET_HEADER, "")
            if provided != self._shared_secret:
                return web.json_response(
                    {"ok": False, "error": "unauthorized"}, status=401
                )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid json"}, status=400
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "error": "expected object"}, status=400
            )
        reply_to = body.get("reply_to")
        content = body.get("content")
        if not isinstance(content, str):
            return web.json_response(
                {"ok": False, "error": "reply_to and content required"},
                status=400,
            )
        raw_chat = body.get("chat_id")
        chat_id = raw_chat if isinstance(raw_chat, str) else ""
        now = time.monotonic()
        if chat_id:
            # Any send for the chat proves the gateway is working on it.
            self._chat_activity[chat_id] = now
        # The turn's last tool call lands in the activity file just before its
        # answer; read it now so that call is reported ahead of the reply,
        # not on the supervisor's next tick.
        self._poll_tool_activity(now)

        if not reply_to:
            self._note_progress(chat_id)
            metadata = body.get("metadata")
            if (
                isinstance(metadata, dict)
                and metadata.get("notify") is True
                and not metadata.get("_interim_send")
            ):
                # hermes marks a final notify (base._mark_notify_metadata) and a
                # mid-run send _interim_send. A final with no anchor answered a
                # run hermes started itself, or one of ours that ran inside it.
                return await self._take_unanchored_final(chat_id, content)
            # Mid-run output: interim text, the "still working" notice, a
            # compaction notice. Liveness only; refusing it (the old 400) only
            # made hermes log a failed send.
            return web.json_response({"ok": True, "progress": True})
        reply_to = str(reply_to)

        future = self._pending.get(reply_to)
        if future is not None and not future.done():
            future.set_result(content)
            self._mark_delivered(reply_to, content)
            turn = self._turns.get(reply_to)
            if turn is not None:
                if turn.command in _INTERRUPTING_COMMANDS:
                    self._end_running_turn(
                        turn.chat_id, f"[the turn was interrupted by {turn.command}]",
                        only=turn.target,
                    )
                if turn.detached:
                    return await self._deliver_late(reply_to, content)
            return web.json_response({"ok": True})

        # Orphan path: no pending future, OR a present-but-done future (a
        # racing earlier delivery — e.g. the busy-interrupt ack — already
        # resolved it, so THIS content will never reach execute(); marking
        # it delivered here would silently bury it, review wf_7cb5003d #2).
        # The gateway's busy-interrupt flow answers the pending future with
        # the "⚡ Interrupting current task" ack, then delivers the REAL
        # reply seconds later under the same reply_to. Dropping such
        # orphans loses the agent's answer forever (2026-07-22: canvas
        # stuck on the interrupt ack while the actual 2176-char reply was
        # discarded as "unknown message_id"). Relay genuinely new content
        # to the canvas via the platform's /notify (AgentMessageWriter
        # SSOT) — but ONLY for human canvas turns: a late reply to a
        # PEER-delegated turn does not belong on the user's chat (#5);
        # peers get the timeout signal and own their retries.
        if self._already_delivered(reply_to, content):
            logger.info(
                "hermes plugin: duplicate reply for message_id=%s — dropped",
                reply_to,
            )
            return web.json_response({"ok": True, "stale": True})
        return await self._deliver_late(reply_to, content)

    async def _deliver_late(self, reply_to: str, content: str) -> "web.Response":
        """An answer nobody is waiting for: deliver it where its origin belongs.

        A person (canvas or channel): the workspace's chat via /notify. An
        idle-digest wake: the same chat, through the digest's own reply
        forwarder, which drops "(idle)" and other housekeeping text, as it would
        have on time. Any other self-wake (scheduler tick, delegation results,
        platform wakes), a peer, a control message, or an origin no longer
        known: not relayed. Their senders never show the answer to a person, and
        the peer already got its interim reply.
        """
        origin, source_type = self._origin_entry(reply_to)
        person = origin in ("user", "channel")
        relay = person or (origin == "self" and source_type == "self-idle")
        if not relay:
            logger.warning(
                "hermes plugin: late reply for message_id=%s origin=%s source=%s — not relayed",
                reply_to,
                origin or "unknown",
                source_type or "-",
            )
            return web.json_response({"ok": True, "stale": True})
        # RESERVE the dedup slot BEFORE awaiting the relay so a concurrent
        # duplicate delivery cannot double-post while the POST is in
        # flight (#6); roll the reservation back on failure and answer
        # 503 so the plugin's retry loop redelivers — a 200 here would
        # make one transient platform blip a permanent reply loss (#3).
        self._mark_delivered(reply_to, content)
        if person:
            relayed = await self._relay_orphan_reply(reply_to, content)
        else:
            status = await self._forward_self_reply(content)
            logger.info(
                "hermes plugin: late reply for self-turn message_id=%s: %s",
                reply_to, status,
            )
            if status in ("suppressed",) or status.startswith("skipped"):
                return web.json_response({"ok": True, "stale": True})
            relayed = status == "delivered"
        if not relayed:
            self._unmark_delivered(reply_to, content)
            return web.json_response(
                {"ok": False, "stale": True, "relayed": False},
                status=503,
            )
        return web.json_response({"ok": True, "stale": True, "relayed": True})

    async def _forward_self_reply(self, content: str) -> str:
        """Hand a self-turn's late reply to the idle digest's reply forwarder."""
        try:
            from molecule_runtime.idle_digest.reply_forwarder import (
                forward_reply_to_user,
            )
        except Exception:  # noqa: BLE001
            return "skipped: reply forwarder unavailable"
        workspace_id = (
            os.environ.get("MOLECULE_WORKSPACE_ID")
            or os.environ.get("WORKSPACE_ID", "")
        ).strip()
        base = (os.environ.get("PLATFORM_URL") or "").strip().rstrip("/")
        if not workspace_id or not base:
            return "skipped: PLATFORM_URL/MOLECULE_WORKSPACE_ID unset"
        try:
            return await forward_reply_to_user(base, workspace_id, content)
        except Exception as exc:  # noqa: BLE001 — it never raises; be sure
            return f"error: {exc}"

    async def _take_unanchored_final(self, chat_id: str, content: str) -> "web.Response":
        """A final with no reply_to. While one of our turns runs it may be that
        turn's answer (hermes ran it inside a run it started itself), so it is
        held; the turn's own answer, or the settle window, decides. With nothing
        running it is the agent speaking on its own: relayed to the chat."""
        running = self._running_turn(chat_id) if chat_id else None
        if running is not None:
            running.unanchored.append(content)
            logger.info(
                "hermes plugin: final with no reply_to on %s while message_id=%s runs; "
                "held (%d)", chat_id, running.message_id, len(running.unanchored),
            )
            return web.json_response({"ok": True, "held": True})
        status = await self._relay_unanchored(chat_id, content)
        if status.startswith("error"):
            return web.json_response({"ok": False, "relayed": False}, status=503)
        return web.json_response({"ok": True, "relayed": status == "delivered"})

    async def _relay_unanchored(self, chat_id: str, content: str) -> str:
        """Post the agent's own output to the chat, through the idle digest's
        forwarder (it drops silence and housekeeping text); the forwarder's status."""
        key = f"unanchored:{chat_id}"
        if self._already_delivered(key, content, within=_UNANCHORED_DEDUP_SECONDS):
            return "duplicate"
        self._mark_delivered(key, content)
        status = await self._forward_self_reply(content)
        logger.info("hermes plugin: final with no reply_to on %s: %s", chat_id or "-", status)
        if status.startswith("error"):
            self._unmark_delivered(key, content)
        return status

    async def _relay_held(self, chat_id: str, texts: List[str]) -> None:
        for text in texts:
            await self._relay_unanchored(chat_id, text)

    # ------------------------------------------------------------------
    # Turn supervision
    # ------------------------------------------------------------------
    def _chat_turns(self, chat_id: str) -> List[_Turn]:
        return [t for t in self._turns.values() if t.chat_id == chat_id]

    def _running_turn(self, chat_id: str) -> Optional[_Turn]:
        """The turn holding the chat's slot at the gateway, once posted."""
        turn = self._turns.get(self._slot.get(chat_id, ""))
        if turn is None or turn.kind != "turn" or not turn.dispatched:
            return None
        future = self._pending.get(turn.message_id)
        return turn if future is not None and not future.done() else None

    def _note_progress(self, chat_id: str) -> None:
        running = self._running_turn(chat_id) if chat_id else None
        if running is not None:
            running.progress += 1
            now = time.monotonic()
            # A run that makes no tool calls for minutes still has to show up as
            # activity on the platform, or its stall watchdog restarts the
            # workspace (see _PROGRESS_ROW_SECONDS).
            if (
                now - (running.started_at or now) >= _PROGRESS_ROW_SECONDS
                and now - self._activity_row_at >= _PROGRESS_ROW_SECONDS
            ):
                self._activity_row_at = now
                minutes = int((now - (running.started_at or now)) // 60)
                self._spawn(emit_tool_call("hermes", f"⏳ still working — {minutes} min", "ok"))
        _touch_turn_lease()

    def _should_skip_self_ping(self, context: RequestContext, text: str) -> bool:
        policy = getattr(self, "_turn_policy", None)
        if policy is None or not policy.drop_self_pings:
            return False
        chat_id = self._derive_chat_id(context)
        if not self._chat_busy(chat_id):
            return False
        if not _is_droppable_self_ping(context, text):
            return False
        logger.info(
            "hermes plugin: routine self-wake (%s) while a turn is in flight on "
            "%s — fast-ack + drop (recurs next cycle)",
            _message_source_type(context) or "untyped", chat_id,
        )
        return True

    # --- busy signals -------------------------------------------------
    # active_tasks (heartbeat) gates the runtime's idle loop; the runtime
    # inbox's turn_in_flight is what the heartbeat reports as is_busy, which
    # the platform reads before it defers, queues or hibernates.
    def _claim_busy(self, turn: _Turn) -> None:
        if turn.counted:
            return
        turn.counted = True
        self._busy_chats[turn.chat_id] = self._busy_chats.get(turn.chat_id, 0) + 1
        self._apply_inbox(turn.chat_id)
        self._spawn(self._push_busy(turn.chat_id, turn.brief))

    def _release_busy(self, turn: _Turn) -> None:
        if not turn.counted:
            return
        turn.counted = False
        remaining = self._busy_chats.get(turn.chat_id, 0) - 1
        if remaining > 0:
            self._busy_chats[turn.chat_id] = remaining
        else:
            self._busy_chats.pop(turn.chat_id, None)
        self._apply_inbox(turn.chat_id)
        self._spawn(self._push_busy(turn.chat_id, ""))

    async def _push_busy(self, chat_id: str, task: str) -> None:
        # set_current_task updates active_tasks before its first await, so the
        # increments and decrements land in the order these tasks were spawned;
        # only the HTTP pushes behind them may overlap, and the next periodic
        # heartbeat carries the settled count.
        await _set_current_task(self._heartbeat, task)
        try:
            from molecule_runtime.runtime_inbox import get_inbox

            await get_inbox().get_or_create(f"hermes:{chat_id}")
        except Exception:  # noqa: BLE001
            return
        self._apply_inbox(chat_id)

    def _apply_inbox(self, chat_id: str) -> None:
        """Mirror the chat's busy state onto its runtime inbox entry, if it exists yet."""
        try:
            from molecule_runtime.runtime_inbox import get_inbox

            entry = get_inbox().peek(f"hermes:{chat_id}")
        except Exception:  # noqa: BLE001
            return
        if entry is None:
            return
        busy = [t for t in self._chat_turns(chat_id) if t.counted]
        entry.turn_in_flight = bool(busy)
        entry.in_flight_source_type = (busy[0].source_type or None) if busy else None

    def _spawn(self, coro) -> Optional[asyncio.Task]:
        if getattr(self, "_stopping", False):
            coro.close()
            return None
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return None
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # --- the supervisor -------------------------------------------------
    def _ensure_supervisor(self) -> None:
        if not self._use_plugin or getattr(self, "_stopping", False):
            return
        task = self._supervisor
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if task is None:
            self._baseline_tool_activity()
        else:
            logger.error("hermes turn supervisor had stopped; restarting it")
        self._supervisor = loop.create_task(self._supervise_loop())

    async def _supervise_loop(self) -> None:
        # A supervisor restarted from inside a turn would inherit its caller.
        _TURN_CALLER.set(None)
        while True:
            await asyncio.sleep(self._turn_policy.poll_seconds)
            try:
                await self._supervise_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("hermes turn supervisor: tick failed")

    async def _supervise_once(self) -> None:
        now = time.monotonic()
        self._poll_tool_activity(now)
        policy = self._turn_policy
        for chat_id in list(self._slot):
            turn = self._running_turn(chat_id)
            if turn is None or turn.stopping:
                continue
            started = turn.started_at or now
            quiet = now - self._last_sign(turn, now)
            if quiet >= policy.stall_seconds:
                self._stop_running(
                    turn, "stall",
                    "[hermes plugin reply timeout: no activity from the hermes gateway "
                    f"for {policy.stall_seconds:g}s; the turn was abandoned]",
                )
            elif policy.max_seconds and now - started >= policy.max_seconds:
                self._stop_running(
                    turn, "cap",
                    "[hermes plugin reply timeout: the turn ran past the "
                    f"{policy.max_seconds:g}s turn cap and was stopped]",
                )
            elif (
                policy.settle_seconds
                and quiet >= policy.settle_seconds
                and now - turn.probed_at >= policy.settle_seconds
            ):
                turn.probed_at = now
                self._spawn(self._settle_if_over(turn))
        self._prune_bookkeeping(now)

    def _last_sign(self, turn: _Turn, now: float) -> float:
        """When the gateway last showed it was working on this turn's chat."""
        return max(
            self._chat_activity.get(turn.chat_id, 0.0),
            self._tool_activity_at,
            turn.started_at or now,
        )

    async def _settle_if_over(self, turn: _Turn) -> None:
        """The gateway has been quiet past the settle window while this turn ran.

        A live run sends "still working" every HERMES_AGENT_NOTIFY_INTERVAL, so if
        the gateway still answers its health check, no run is going and nothing
        more will come under this turn's id (see the turn-supervision notes). The
        turn is settled with the last final held for it, or with _NO_RESPONSE.
        A gateway that does not answer is left to the stall window.
        """
        if not await self._gateway_alive():
            return
        future = self._pending.get(turn.message_id)
        if future is None or future.done() or self._running_turn(turn.chat_id) is not turn:
            return
        now = time.monotonic()
        quiet = now - self._last_sign(turn, now)
        if quiet < self._turn_policy.settle_seconds:
            return  # the gateway spoke while it was being asked
        held = bool(turn.unanchored)
        answer = turn.unanchored.pop() if held else _NO_RESPONSE
        logger.warning(
            "hermes turn %s: the gateway was idle for %.0fs with no answer under its id; "
            "settled with %s",
            turn.message_id, quiet,
            "the last final it sent without one" if held else "no response",
        )
        self._mark_delivered(turn.message_id, answer)
        future.set_result(answer)
        if turn.detached and held:
            await self._deliver_late(turn.message_id, answer)

    async def _gateway_alive(self) -> bool:
        url = f"http://{self._plugin_host}:{self._plugin_port}/a2a/health"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    def _stop_running(self, turn: _Turn, reason: str, text: str) -> None:
        """End the running turn: /stop, then settle it, then the queue moves on.

        Only the running turn is settled. The messages waiting behind it never
        reached the gateway and each gets its own run next. The slot is handed on
        only after /stop is answered, so the next message cannot land in hermes
        behind a run that is still being stopped.
        """
        turn.stopping = True
        logger.error(
            "hermes plugin: %s on %s — message_id=%s running %.0fs; sending /stop",
            reason, turn.chat_id, turn.message_id,
            time.monotonic() - (turn.started_at or turn.registered_at),
        )
        self._spawn(self._stop_then_settle(turn, text))

    async def _stop_then_settle(self, turn: _Turn, text: str) -> None:
        _TURN_CALLER.set(None)  # this /stop belongs to no caller's deadline
        await self._send_stop(turn.chat_id)
        future = self._pending.get(turn.message_id)
        if future is None or future.done():
            return
        if turn.detached and turn.origin in ("user", "channel"):
            # The person was told the answer would follow; tell them it won't.
            await self._relay_orphan_reply(turn.message_id, text)
        self._fail(turn, future, text)

    def _end_running_turn(self, chat_id: str, text: str, *, only: str) -> None:
        """A person's /stop, /new or /reset ended the running turn in hermes:
        the turn ``only`` names, the one running when the command went out. A
        turn that started since then is not the one the person meant."""
        turn = self._running_turn(chat_id)
        if turn is None or not only or turn.message_id != only:
            return
        future = self._pending.get(turn.message_id)
        if future is not None:
            logger.info("hermes plugin: %s (message_id=%s)", text, turn.message_id)
            self._fail(turn, future, text)

    async def _send_stop(self, chat_id: str) -> None:
        try:
            reply = await self._dispatch_and_wait(
                content="/stop",
                chat_id=chat_id,
                peer_id="",
                peer_name=None,
                callback_url=(
                    f"http://{self._callback_host}:{self._callback_port}"
                    f"{_DEFAULT_CALLBACK_PATH}"
                ),
                history=None,
                event_queue=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("hermes plugin: /stop dispatch failed for %s", chat_id)
            return
        if reply is None:
            logger.warning(
                "hermes plugin: /stop on %s got no answer; the run may still be going",
                chat_id,
            )
            return
        logger.info("hermes plugin: /stop on %s answered: %s", chat_id, reply[:120])

    # --- tool events from the hermes gateway --------------------------
    # The molecule_turn_activity hermes plugin rewrites the activity file after
    # every tool call. Each new event becomes one agent_log activity row: the
    # canvas tool chip, and the activity that keeps the platform's stall
    # watchdog from restarting a workspace whose long turn is marked busy.
    def _baseline_tool_activity(self) -> None:
        path = os.environ.get("MOLECULE_TOOL_ACTIVITY_FILE", "").strip()
        if not path:
            return
        try:
            self._tool_file_mtime = os.stat(path).st_mtime
        except OSError:
            return
        record = _read_tool_event(path)
        if record is not None:
            self._tool_event_mark = (record.get("boot"), record["seq"])

    def _poll_tool_activity(self, now: float) -> None:
        path = os.environ.get("MOLECULE_TOOL_ACTIVITY_FILE", "").strip()
        if not path:
            return
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return
        if mtime <= self._tool_file_mtime:
            return
        self._tool_file_mtime = mtime
        self._tool_activity_at = now
        record = _read_tool_event(path)
        if record is None:
            return
        mark = (record.get("boot"), record["seq"])
        previous = self._tool_event_mark
        if previous is not None and previous[0] == mark[0] and mark[1] <= previous[1]:
            return
        self._tool_event_mark = mark
        tool = record["tool"].strip()
        if not tool:
            return
        status = "error" if record.get("status") == "error" else "ok"
        args = record.get("args") if isinstance(record.get("args"), str) else ""
        self._activity_row_at = now
        self._spawn(emit_tool_call(tool, _tool_chip_summary(tool, args), status))

    # Retry-dedup bookkeeping for _handle_reply. TTL bounds memory AND
    # bounds the redelivery-dedup horizon: a retry arriving after expiry
    # (or after cap eviction) relays again, so both bounds are sized well
    # above any plausible adapter retry/redelivery window (review
    # wf_7cb5003d #7) and the cap evicts OLDEST-BY-AGE, never a fresh
    # entry that still guards an in-flight retry cycle.
    _DELIVERED_TTL = 3600.0
    _DELIVERED_MAX = 2048

    @staticmethod
    def _content_key(message_id: str, content: str) -> tuple:
        import hashlib

        digest = hashlib.sha256(
            content.encode("utf-8", errors="replace")
        ).hexdigest()
        return (str(message_id), digest)

    def _mark_delivered(self, message_id: str, content: str) -> None:
        import time

        now = time.monotonic()
        self._delivered[self._content_key(message_id, content)] = now
        self._prune_bookkeeping(now)

    def _unmark_delivered(self, message_id: str, content: str) -> None:
        self._delivered.pop(self._content_key(message_id, content), None)

    def _prune_bookkeeping(self, now: float) -> None:
        cutoff = now - self._DELIVERED_TTL
        for k, ts in list(self._delivered.items()):
            if ts < cutoff:
                self._delivered.pop(k, None)
        while len(self._delivered) > self._DELIVERED_MAX:
            oldest = min(self._delivered, key=self._delivered.get)
            self._delivered.pop(oldest, None)
        # Origins outlive the dedup window: a turn can run for the whole cap
        # before its answer shows up. Entries are (origin, ts, source_type).
        turns = getattr(self, "_turns", {})
        late_cutoff = now - _LATE_REPLY_TTL
        for m, entry in list(self._origin.items()):
            if entry[1] < late_cutoff and m not in turns:
                self._origin.pop(m, None)
        while len(self._origin) > self._DELIVERED_MAX:
            oldest = min(self._origin, key=lambda m: self._origin[m][1])
            self._origin.pop(oldest, None)
        activity = getattr(self, "_chat_activity", {})
        live_chats = {t.chat_id for t in turns.values()}
        for chat_id, ts in list(activity.items()):
            if ts < late_cutoff and chat_id not in live_chats:
                activity.pop(chat_id, None)

    def _record_origin(self, message_id: str, origin: str, source_type: str = "") -> None:
        import time

        now = time.monotonic()
        self._origin[str(message_id)] = (origin, now, source_type or "")
        self._prune_bookkeeping(now)

    def _origin_entry(self, message_id: str) -> tuple:
        """(origin, source_type) recorded for the message, or ("", "")."""
        entry = self._origin.get(str(message_id))
        if not entry:
            return ("", "")
        return (entry[0], entry[2] if len(entry) > 2 else "")

    def _origin_of(self, message_id: str) -> str:
        return self._origin_entry(message_id)[0]

    def _already_delivered(
        self, message_id: str, content: str, within: Optional[float] = None
    ) -> bool:
        import time

        ts = self._delivered.get(self._content_key(message_id, content))
        window = self._DELIVERED_TTL if within is None else within
        return ts is not None and (time.monotonic() - ts) < window

    async def _relay_orphan_reply(self, reply_to: str, content: str) -> bool:
        """Push a late gateway reply to the canvas chat via /notify.

        Uses the platform's sanctioned agent→user path (AgentMessageWriter:
        broadcast + durable activity row) so the message both renders live
        and survives reload. Never raises — a relay failure must not break
        the reply endpoint; the plugin already got its 200.
        """
        if not content.strip():
            return False
        workspace_id = (
            os.environ.get("MOLECULE_WORKSPACE_ID")
            or os.environ.get("WORKSPACE_ID", "")
        ).strip()
        base = (os.environ.get("PLATFORM_URL") or "").strip().rstrip("/")
        if not workspace_id or not base:
            logger.warning(
                "hermes plugin: late reply for message_id=%s NOT relayed — "
                "PLATFORM_URL/MOLECULE_WORKSPACE_ID unset",
                reply_to,
            )
            return False
        headers: Dict[str, str] = {}
        try:
            from molecule_runtime.platform_auth import auth_headers

            headers = dict(auth_headers() or {})
        except Exception:
            pass
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{base}/workspaces/{workspace_id}/notify",
                    json={"message": content},
                    headers=headers,
                )
            ok = 200 <= resp.status_code < 300
            log = logger.info if ok else logger.warning
            log(
                "hermes plugin: late reply for message_id=%s relayed via "
                "/notify (status=%s)",
                reply_to,
                resp.status_code,
            )
            if ok:
                self._mark_delivered(reply_to, content)
            return ok
        except Exception as exc:
            logger.warning(
                "hermes plugin: late-reply relay for message_id=%s failed: %s",
                reply_to,
                exc,
            )
            return False

    @staticmethod
    def _derive_chat_id(context: RequestContext) -> str:
        # RFC #600 layer-2 hot-patch (task #262). PR #35 corrected the
        # priority tuple to context_id-first, which is mechanically right
        # — but the a2a-sdk's RequestContext._check_or_generate_context_id
        # auto-mints a FRESH UUID per turn when the inbound message lacks
        # a context_id, and the platform's POST /workspaces/<id>/a2a does
        # not yet thread a canvas-side conversation key through. Result:
        # context_id IS populated, but it changes every turn, so the
        # hermes daemon's SessionStore still creates a new session per
        # turn (empirically: 6 separate sessions in 3 minutes in
        # /tmp/.hermes/sessions/sessions.json, diagnosis a60623344).
        #
        # Until the proper fix lands (RFC #600 layer-2 platform-side
        # context_id propagation, separate PR), key chat_id on the
        # workspace's own ID — it's stable per-workspace by definition,
        # and a canvas chat is one-to-one with a workspace. This
        # collapses all turns in the same workspace into one session,
        # which is the desired UX.
        workspace_id = os.environ.get("WORKSPACE_ID")
        if workspace_id:
            return f"workspace:{workspace_id}"
        # Defensive fallback: preserve PR #35's context_id-first chain
        # for environments without WORKSPACE_ID (local dev, peer-agent
        # turns from non-canvas sources). Backwards-compat with all
        # prior shapes.
        for attr in ("context_id", "session_id", "task_id"):
            value = getattr(context, attr, None)
            if value:
                return str(value)
        message = getattr(context, "message", None)
        if message is not None:
            for attr in ("context_id", "session_id", "task_id", "messageId"):
                value = getattr(message, attr, None)
                if value:
                    return str(value)
        return f"adhoc-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _derive_peer_identity(context: RequestContext) -> tuple[str, str]:
        """Pull the sender's (peer_id, peer_name) off context.message.metadata.

        Platform-side conventions (molecule MCP protocol):
          * canvas_user → peer_id is the empty string. peer_name may be
            absent or carry a user-facing display string the platform
            attached (rare today; reserved).
          * peer_agent  → peer_id is the sender workspace's UUID, and
            peer_name is its display name from the registry.

        We read off ``context.message.metadata`` if present and tolerate
        missing/malformed shapes (return empty strings rather than
        raise). The previous behavior — aliasing chat_id into both
        fields — surfaced in canvas as hermes greeting the user with
        ``your peer ID is <uuid>`` (task #262). Default to empty so the
        daemon's system prompt doesn't leak chat_id as the peer label.
        """
        message = getattr(context, "message", None)
        if message is None:
            return ("", "")
        metadata = getattr(message, "metadata", None) or {}
        if not isinstance(metadata, dict):
            return ("", "")
        peer_id = metadata.get("peer_id") or ""
        peer_name = metadata.get("peer_name") or ""
        if not isinstance(peer_id, str):
            peer_id = ""
        if not isinstance(peer_name, str):
            peer_name = ""
        return (peer_id, peer_name)

    @staticmethod
    def _derive_source_type(context: RequestContext) -> str:
        """The delivery's autonomy marker, or "" for an interactive turn.

        The scheduler plugin stamps ``metadata.source_type="self-scheduler"``
        on a trigger turn so the receiving agent can tell an autonomous
        self-ping from a human message. We dropped it here: every field the
        plugin transport forwards is enumerated by hand in
        ``_dispatch_and_wait``'s payload, and ``source_type`` was never added,
        so a scheduled delivery arrived at hermes indistinguishable from a
        person typing. That is one half of the ~305s scheduled-run hang: with
        no marker, a flagged command opens an approval nobody is listening for
        and the turn blocks until the approval timeout.

        Returns "" for anything not in ``_SCHEDULED_SOURCE_TYPES`` — see the
        note there for why this is an allow-list and not a passthrough.

        Read with the runtime's own extractor: message metadata first, then the
        request-level metadata where the runtime's self-sends (the scheduler's
        trigger lane among them) put the marker. Reading only the message
        level missed every one of those.
        """
        source_type = _message_source_type(context)
        return source_type if source_type in _SCHEDULED_SOURCE_TYPES else ""

    # ------------------------------------------------------------------
    # Legacy chat_completions transport (fallback)
    # ------------------------------------------------------------------
    async def _execute_via_chat_completions(
        self,
        event_queue: EventQueue,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        peers_blurb = await self._fetch_peers_blurb()
        messages = self._build_initial_messages(
            prompt, peers_blurb, history=history
        )
        headers = self._build_chat_completions_headers()

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                for _round in range(_MAX_TOOL_ROUNDS):
                    payload: dict[str, Any] = {
                        "model": self._config.model or "hermes-agent",
                        "messages": messages,
                        "stream": False,
                    }
                    if _ACTIVE_TOOLS:
                        payload["tools"] = _ACTIVE_TOOLS
                    resp = await client.post(
                        f"{self._base}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    choice = data.get("choices", [{}])[0]
                    finish = choice.get("finish_reason", "stop")
                    assistant_msg = choice.get("message", {})

                    if finish == "tool_calls":
                        messages.append(assistant_msg)
                        tool_calls = assistant_msg.get("tool_calls") or []
                        for tc in tool_calls:
                            result = await self._dispatch_tool(tc)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": result,
                            })
                    else:
                        text = assistant_msg.get("content")
                        if not text:
                            text = self._extract_assistant_text(data)
                        await event_queue.enqueue_event(new_text_message(text))
                        return

                # Exhausted tool rounds — emit whatever we have
                text = self._extract_assistant_text(data)
                await event_queue.enqueue_event(new_text_message(text))

        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            logger.error("hermes-agent %s: %s", exc.response.status_code, body)
            await event_queue.enqueue_event(
                new_text_message(
                    f"[hermes-agent error {exc.response.status_code}] {body}"
                )
            )
        except httpx.RequestError as exc:
            logger.exception("hermes-agent transport error")
            await event_queue.enqueue_event(
                new_text_message(f"[hermes-agent unreachable] {exc!s}")
            )

    def _build_initial_messages(
        self,
        user_text: str,
        peers_blurb: str = "",
        history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        system_content = self._config.system_prompt or ""
        if peers_blurb:
            system_content = (
                f"{system_content}\n\n{peers_blurb}".strip()
                if system_content
                else peers_blurb
            )
        messages: list[dict[str, Any]] = []
        if system_content:
            messages.append({"role": "system", "content": system_content})

        # Replay prior canvas turns between system and the current user
        # turn so hermes-agent has conversational context. Canvas ships
        # the last 20 turns under metadata.history as
        #   {"role": "user"|"agent",
        #    "parts": [{"kind": "text", "text": "..."}]}
        # (see canvas/src/components/tabs/chat/hooks/useChatSend.ts:104-111
        # in molecule-core). We translate to OpenAI chat-completions shape
        # (assistant role + flat string content). Unknown roles are skipped
        # with a warning rather than smuggled in as raw text.
        replayed = 0
        if history:
            for turn in history:
                if not isinstance(turn, dict):
                    continue
                src_role = turn.get("role")
                if src_role == "user":
                    dst_role = "user"
                elif src_role == "agent":
                    dst_role = "assistant"
                else:
                    logger.warning(
                        "hermes history: skipping turn with unknown role=%r",
                        src_role,
                    )
                    continue
                content = self._extract_history_text(turn)
                if not content:
                    continue
                messages.append({"role": dst_role, "content": content})
                replayed += 1

        messages.append({"role": "user", "content": user_text})

        if replayed:
            logger.info(
                "hermes_history_prepended turns=%d source=%s",
                replayed,
                "canvas_metadata",
            )
        return messages

    @staticmethod
    def _extract_history_text(turn: dict[str, Any]) -> str:
        """Flatten a canvas A2A history turn into chat-completions text.

        Canvas turns carry text inside `parts: [{kind: "text", text: "..."}]`.
        Tolerate string-shaped content too in case a peer pre-flattens."""
        content = turn.get("content")
        if isinstance(content, str) and content:
            return content
        parts = turn.get("parts") or []
        chunks: list[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("kind") == "text":
                t = p.get("text")
                if isinstance(t, str) and t:
                    chunks.append(t)
        return "\n".join(chunks)

    @staticmethod
    def _extract_history_from_context(
        context: RequestContext,
    ) -> list[dict[str, Any]]:
        """Pull the canvas-shipped history array off context.message.metadata.

        Tolerates missing metadata / wrong types — falls through to an
        empty list so a malformed envelope just yields the legacy
        no-history behavior rather than crashing the turn."""
        message = getattr(context, "message", None)
        if message is None:
            return []
        metadata = getattr(message, "metadata", None) or {}
        if not isinstance(metadata, dict):
            return []
        history = metadata.get("history")
        if not isinstance(history, list):
            return []
        return history

    def _build_chat_completions_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("API_SERVER_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ------------------------------------------------------------------
    # Molecule platform tool helpers
    # ------------------------------------------------------------------
    async def _fetch_peers_blurb(self) -> str:
        """Return a formatted peer list for system-prompt injection.
        Calls the shared Molecule MCP dispatcher directly."""
        if not _MOLECULE_TOOLS_AVAILABLE:
            return ""
        try:
            result = await handle_molecule_tool_call("list_peers", {})
            if not result or "No peers" in result:
                return ""
            return (
                "## Molecule platform peers\n"
                "The following peer workspaces are reachable via the Molecule "
                "A2A platform. Use list_peers, delegate_task, or delegate_task_async "
                "to interact with them.\n"
                + result
            )
        except Exception:
            logger.debug("could not fetch peers for system prompt", exc_info=True)
            return ""

    async def _dispatch_tool(self, tool_call: dict[str, Any]) -> str:
        """Dispatch a model-requested tool call to the Molecule platform."""
        # RC #203 (tier-C liveness): a tool call is liveness. Bump the exported
        # activity file so a long tool-running turn isn't mistaken for a stall.
        # No-op when MOLECULE_TOOL_ACTIVITY_FILE is unset (off-kernel).
        _record_tool_activity()
        fn = tool_call.get("function") or {}
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "{}")
        try:
            args: dict[str, Any] = (
                json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            )
        except json.JSONDecodeError:
            args = {}

        # ADR-004 shared tool-call display: render this invocation as a
        # ToolTraceChip on the canvas by POSTing an `agent_log` row via the
        # engine-owned emit_tool_call primitive (method=<tool_name>). Fire it
        # BEFORE dispatch so the chip appears as the tool starts, mirroring
        # claude-code's on_tool_start emit. The primitive is fire-and-forget
        # and swallows every exception, so it can never abort the tool loop.
        #
        # NOTE (plugin-transport follow-up): this only fires on the in-process
        # chat-completions fallback path (MOLECULE_A2A_PLATFORM_ENABLED=false).
        # In PROD hermes runs the PLUGIN transport (Dockerfile default true) —
        # the tool loop lives inside the SEPARATE hermes-agent repo, which
        # returns only {content} over /a2a/reply, so no per-tool events reach
        # this executor and this emit is effectively dormant in prod. Closing
        # the user-visible gap on the plugin path requires hermes-agent to
        # either emit these `agent_log` rows itself or include a per-tool trace
        # array in its /a2a/reply callback body (mirroring this same
        # {activity_type:'agent_log', method:<tool>, summary} contract). That
        # is a separate-repo change and is deliberately OUT OF SCOPE here.
        await emit_tool_call(name, summarize_tool(name, args))

        if not _MOLECULE_TOOLS_AVAILABLE:
            return f"Tool {name!r} unavailable: molecule_runtime.mcp_tools not installed."

        try:
            return await handle_molecule_tool_call(name, args)
        except Exception as exc:
            return f"Tool error ({name}): {exc!s}"

    @staticmethod
    def _extract_assistant_text(data: dict[str, Any]) -> str:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            logger.warning("Unexpected hermes-agent response shape: %r", data)
            return "(hermes-agent returned no content)"

        content = message.get("content") or ""
        if content:
            return content

        # Reasoning models (MiniMax M2/M2.7, Moonshot Kimi K2.6) on their
        # OpenAI-compatible endpoint return the assistant turn in a separate
        # ``reasoning_content`` field and leave ``content`` empty when the
        # whole turn was reasoning (e.g. a tight max_tokens budget consumed
        # by the thinking preamble). Treating that as "no content" surfaced a
        # genuine reply as an empty A2A turn (issue #2204 — staging canary
        # red, and any real agent on a reasoning model). Fall back to it so
        # the turn isn't seen as empty.
        reasoning = message.get("reasoning_content") or ""
        if reasoning:
            return reasoning

        # content AND reasoning_content both empty → a genuinely empty/error
        # reply, NOT a reasoning-only turn. Keep the existing sentinel so this
        # stays distinguishable from a real answer (do not mask it).
        logger.warning("hermes-agent returned empty content and no reasoning: %r", data)
        return "(hermes-agent returned no content)"
