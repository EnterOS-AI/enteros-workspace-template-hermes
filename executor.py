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
    from molecule_runtime.a2a_tools import (
        tool_check_task_status,
        tool_commit_memory,
        tool_delegate_task,
        tool_delegate_task_async,
        tool_get_workspace_info,
        tool_list_peers,
        tool_recall_memory,
        tool_send_message_to_user,
    )
    _A2A_TOOLS_AVAILABLE = True
except ImportError:
    _A2A_TOOLS_AVAILABLE = False

logger = logging.getLogger(__name__)


# --- legacy chat-completions config -----------------------------------
_DEFAULT_BASE = "http://127.0.0.1:8642/v1"
_REQUEST_TIMEOUT = 600.0

# --- Molecule platform tools -----------------------------------------------
# Injected into every chat-completions call so hermes-agent can use the full
# Molecule A2A toolset: peer discovery, task delegation, memory, etc.
# Mirrors the tools exposed by the stdio MCP server for Claude Code / Codex.
_MOLECULE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_peers",
            "description": "List all peer workspaces reachable via the Molecule A2A platform. Returns name, ID, status, and role for each peer.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate a task to another workspace via A2A (synchronous — waits for the peer's response).",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": "Target workspace ID (from list_peers)"},
                    "task": {"type": "string", "description": "Task description to send to the peer"},
                },
                "required": ["workspace_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task_async",
            "description": "Delegate a task to a peer workspace (fire-and-forget). Returns immediately with a task_id; use check_task_status to poll for the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": "Target workspace ID"},
                    "task": {"type": "string", "description": "Task description"},
                },
                "required": ["workspace_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_task_status",
            "description": "Check the status and result of a previously delegated async task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": "Target workspace ID used in delegate_task_async"},
                    "task_id": {"type": "string", "description": "task_id returned by delegate_task_async"},
                },
                "required": ["workspace_id", "task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "Send a direct message to the user's canvas chat (WebSocket push). Use for proactive updates when the user isn't actively polling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message text to deliver to the user"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_info",
            "description": "Get this workspace's own metadata — ID, name, role, tier, parent, status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_memory",
            "description": "Save important information to persistent memory so it can be recalled across conversations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Information to persist"},
                    "scope": {
                        "type": "string",
                        "description": "Visibility: LOCAL (this workspace only, default), TEAM, or GLOBAL",
                        "enum": ["LOCAL", "TEAM", "GLOBAL"],
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search persistent memory for previously saved information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (optional — omit to list recent entries)"},
                    "scope": {"type": "string", "description": "Scope filter: LOCAL, TEAM, or GLOBAL (optional)"},
                },
                "required": [],
            },
        },
    },
]
# Only inject tools if the a2a_tools module loaded successfully.
_ACTIVE_TOOLS: list[dict[str, Any]] = _MOLECULE_TOOLS if _A2A_TOOLS_AVAILABLE else []
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


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


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
            # session_key derived from SessionSource.chat_id). The daemon
            # replays its own transcript on each turn — shipping
            # canvas-side history in the payload is double-bookkeeping
            # and a source of the chloe-dong divergence (RFC #600,
            # sibling to #497). Do NOT extract history here.
            await self._execute_via_plugin(context, event_queue, prompt)
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
    ) -> None:
        message_id = uuid.uuid4().hex
        chat_id = self._derive_chat_id(context)
        callback_url = (
            f"http://{self._callback_host}:{self._callback_port}{_DEFAULT_CALLBACK_PATH}"
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[message_id] = future

        # Per RFC #600: the hermes daemon owns session state natively
        # (gateway/session.py SessionStore, keyed by session_key derived
        # from SessionSource.chat_id). The adapter hands chat_id through;
        # the daemon replays its own transcript on the next turn. Do NOT
        # ship messages_history in the payload — it's double-bookkeeping
        # and a divergence source (sibling to RFC #497).
        payload = {
            "chat_id": chat_id,
            "peer_id": chat_id,
            "peer_name": chat_id,
            "content": prompt,
            "message_id": message_id,
            "callback_url": callback_url,
        }
        headers = {"Content-Type": "application/json"}
        if self._shared_secret:
            headers[SECRET_HEADER] = self._shared_secret

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

        await event_queue.enqueue_event(new_text_message(text))

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
        if future is None:
            # Late or duplicate delivery — the original execute() either
            # already timed out or never registered. Acknowledge so the
            # plugin doesn't retry forever.
            logger.warning(
                "hermes plugin: reply for unknown message_id=%s", reply_to
            )
            return web.json_response({"ok": True, "stale": True})

        if not future.done():
            future.set_result(content)
        return web.json_response({"ok": True})

    @staticmethod
    def _derive_chat_id(context: RequestContext) -> str:
        # Prefer context_id for stable cross-turn identity. Per a2a-sdk
        # semantics, task_id changes per turn (each inbound canvas
        # message creates a fresh task), while context_id is the stable
        # conversation key. Keying chat_id on task_id breaks hermes's
        # SessionStore continuity — every turn becomes a new session,
        # which is exactly the runtime failure RFC #600 set out to fix
        # (sibling to workspace-template-openclaw PR #29, which made
        # the same correction for openclaw's --session-id). Fall back
        # to session_id, then task_id, then message-side attrs; last
        # resort is a synthetic ID so we always pass something hermes
        # can use to key its session store.
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
                        text = assistant_msg.get("content") or ""
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
        Calls the a2a_tools implementation directly (no extra HTTP round-trip)."""
        if not _A2A_TOOLS_AVAILABLE:
            return ""
        try:
            result = await tool_list_peers()
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
        fn = tool_call.get("function") or {}
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "{}")
        try:
            args: dict[str, Any] = (
                json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            )
        except json.JSONDecodeError:
            args = {}

        if not _A2A_TOOLS_AVAILABLE:
            return f"Tool {name!r} unavailable: molecule_runtime.a2a_tools not installed."

        try:
            if name == "list_peers":
                return await tool_list_peers()
            elif name == "delegate_task":
                return await tool_delegate_task(
                    args.get("workspace_id", ""), args.get("task", "")
                )
            elif name == "delegate_task_async":
                return await tool_delegate_task_async(
                    args.get("workspace_id", ""), args.get("task", "")
                )
            elif name == "check_task_status":
                return await tool_check_task_status(
                    args.get("workspace_id", ""), args.get("task_id", "")
                )
            elif name == "send_message_to_user":
                return await tool_send_message_to_user(args.get("message", ""))
            elif name == "get_workspace_info":
                return await tool_get_workspace_info()
            elif name == "commit_memory":
                return await tool_commit_memory(
                    args.get("content", ""), args.get("scope", "LOCAL")
                )
            elif name == "recall_memory":
                return await tool_recall_memory(
                    args.get("query", ""), args.get("scope", "")
                )
            else:
                return f"Unknown tool: {name!r}"
        except Exception as exc:
            return f"Tool error ({name}): {exc!s}"

    @staticmethod
    def _extract_assistant_text(data: dict[str, Any]) -> str:
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            logger.warning("Unexpected hermes-agent response shape: %r", data)
            return "(hermes-agent returned no content)"
