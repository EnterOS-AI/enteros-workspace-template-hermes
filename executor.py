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
race).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from typing import Any, Dict, Optional

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

# Same generous bound as the chat-completions path. Prevents a hung
# hermes daemon from wedging the A2A queue forever.
_PLUGIN_REPLY_TIMEOUT = 600.0


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

    async def stop(self) -> None:
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

        # Cancel all pending futures so any in-flight execute() calls
        # surface a clear shutdown error rather than hanging until the
        # 600s timeout fires.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("executor shutting down"))
        self._pending.clear()

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
            await self._execute_via_plugin(
                context, event_queue, prompt, history=history
            )
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
        # Both transports rely on a wall-clock timeout in execute() to
        # bound a hung hermes-side run. No per-request cancel API is
        # exposed by either path today. Revisit when hermes adds a
        # turn/interrupt RPC for the chat_completions path.
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
        message_id = uuid.uuid4().hex
        chat_id = self._derive_chat_id(context)
        peer_id, peer_name = self._derive_peer_identity(context)
        callback_url = (
            f"http://{self._callback_host}:{self._callback_port}{_DEFAULT_CALLBACK_PATH}"
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[message_id] = future
        self._record_origin(message_id, "peer" if peer_id else "user")

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
            "content": prompt,
            "message_id": message_id,
            "callback_url": callback_url,
        }
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

        # Tool-trace capture (canvas chain parity with claude-code): snapshot
        # the session files before dispatch; the post-reply delta holds the
        # turn's assistant tool_calls.
        session_snapshot = _session_jsonl_snapshot()

        inbound_url = (
            f"http://{self._plugin_host}:{self._plugin_port}/a2a/inbound"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(inbound_url, json=payload, headers=headers)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            self._pending.pop(message_id, None)
            logger.exception("hermes plugin POST failed")
            await event_queue.enqueue_event(
                new_text_message(f"[hermes plugin POST error] {exc!s}")
            )
            return

        try:
            text = await asyncio.wait_for(future, timeout=_PLUGIN_REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(
                "hermes plugin: reply timeout for message_id=%s", message_id
            )
            await event_queue.enqueue_event(
                new_text_message(
                    f"[hermes plugin reply timeout after {_PLUGIN_REPLY_TIMEOUT:.0f}s]"
                )
            )
            return
        except Exception as exc:
            await event_queue.enqueue_event(
                new_text_message(f"[hermes plugin error] {exc!s}")
            )
            return
        finally:
            self._pending.pop(message_id, None)

        await event_queue.enqueue_event(
            _reply_message_with_tool_trace(
                text, _tool_trace_from_session_delta(session_snapshot)
            )
        )

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

        reply_to = body.get("reply_to")
        content = body.get("content")
        if not reply_to or not isinstance(content, str):
            return web.json_response(
                {"ok": False, "error": "reply_to and content required"},
                status=400,
            )

        future = self._pending.get(reply_to)
        if future is not None and not future.done():
            future.set_result(content)
            self._mark_delivered(reply_to, content)
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
        origin = self._origin_of(reply_to)
        if origin != "user":
            # "peer" — or unknown (executor restarted since registration;
            # conservative pre-relay behavior: drop, matching the old
            # uniform-drop contract for turns we cannot attribute).
            logger.warning(
                "hermes plugin: late reply for message_id=%s origin=%s — not relayed",
                reply_to,
                origin or "unknown",
            )
            return web.json_response({"ok": True, "stale": True})
        # RESERVE the dedup slot BEFORE awaiting the relay so a concurrent
        # duplicate delivery cannot double-post while the POST is in
        # flight (#6); roll the reservation back on failure and answer
        # 503 so the plugin's retry loop redelivers — a 200 here would
        # make one transient platform blip a permanent reply loss (#3).
        self._mark_delivered(reply_to, content)
        relayed = await self._relay_orphan_reply(reply_to, content)
        if not relayed:
            self._unmark_delivered(reply_to, content)
            return web.json_response(
                {"ok": False, "stale": True, "relayed": False},
                status=503,
            )
        return web.json_response({"ok": True, "stale": True, "relayed": True})

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
        for m, (_, ts) in list(self._origin.items()):
            if ts < cutoff:
                self._origin.pop(m, None)
        while len(self._origin) > self._DELIVERED_MAX:
            oldest = min(self._origin, key=lambda m: self._origin[m][1])
            self._origin.pop(oldest, None)

    def _record_origin(self, message_id: str, origin: str) -> None:
        import time

        now = time.monotonic()
        self._origin[str(message_id)] = (origin, now)
        self._prune_bookkeeping(now)

    def _origin_of(self, message_id: str) -> str:
        entry = self._origin.get(str(message_id))
        return entry[0] if entry else ""

    def _already_delivered(self, message_id: str, content: str) -> bool:
        import time

        ts = self._delivered.get(self._content_key(message_id, content))
        return ts is not None and (time.monotonic() - ts) < self._DELIVERED_TTL

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
