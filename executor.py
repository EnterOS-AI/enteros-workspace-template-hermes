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
from molecule_runtime.executor_helpers import extract_message_text

logger = logging.getLogger(__name__)


# --- legacy chat-completions config -----------------------------------
_DEFAULT_BASE = "http://127.0.0.1:8642/v1"
_REQUEST_TIMEOUT = 600.0

# --- peer-discovery tool ---------------------------------------------------
# Injected into every chat-completions call so hermes-agent can see and
# explicitly request a fresh Molecule peer list via function calling.
_PEER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_peers",
            "description": (
                "Return the live list of peer workspaces reachable via the "
                "Molecule A2A platform. Each entry includes the peer name, "
                "workspace ID, current status, and role. Call this when you "
                "need to know who you can collaborate with or when the user "
                "asks 'who are your peers / what agents can you see'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]
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
        prompt = extract_message_text(context.message) or ""
        if not prompt.strip():
            await event_queue.enqueue_event(
                new_text_message("(empty prompt — nothing to do)")
            )
            return

        if self._use_plugin:
            await self._execute_via_plugin(context, event_queue, prompt)
        else:
            await self._execute_via_chat_completions(event_queue, prompt)

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
        # Prefer task_id for stable per-conversation identity. Fall back
        # to session/message attributes the a2a-sdk exposes; last resort
        # is a synthetic ID so we always pass something hermes can use
        # to key its session store.
        for attr in ("task_id", "session_id", "context_id"):
            value = getattr(context, attr, None)
            if value:
                return str(value)
        message = getattr(context, "message", None)
        if message is not None:
            for attr in ("task_id", "session_id", "context_id", "messageId"):
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
    ) -> None:
        peers_blurb = await self._fetch_peers_blurb()
        messages = self._build_initial_messages(prompt, peers_blurb)
        headers = self._build_chat_completions_headers()

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                for _round in range(_MAX_TOOL_ROUNDS):
                    payload: dict[str, Any] = {
                        "model": self._config.model or "hermes-agent",
                        "messages": messages,
                        "stream": False,
                        "tools": _PEER_TOOLS,
                    }
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
        self, user_text: str, peers_blurb: str = ""
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
        messages.append({"role": "user", "content": user_text})
        return messages

    def _build_chat_completions_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("API_SERVER_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ------------------------------------------------------------------
    # Peer-discovery helpers
    # ------------------------------------------------------------------
    async def _fetch_peers_blurb(self) -> str:
        """Fetch the live Molecule peer list and return a formatted string
        suitable for injection into the system prompt. Returns empty string
        on any error so the absence of platform creds is non-fatal."""
        peers = await self._fetch_peers_raw()
        if peers is None:
            return ""
        if not peers:
            return "Molecule platform peers: none currently available."
        lines = []
        for p in peers:
            name = p.get("name") or p.get("id", "?")
            pid = p.get("id", "?")
            status = p.get("status", "unknown")
            role = p.get("role") or ""
            entry = f"  - {name} (ID: {pid}, status: {status}"
            if role:
                entry += f", role: {role}"
            entry += ")"
            lines.append(entry)
        return (
            "## Molecule platform peers\n"
            "These peer workspaces are reachable via A2A. "
            "You can reference them by name or ID when the user wants to "
            "collaborate with or delegate to another agent.\n"
            + "\n".join(lines)
        )

    async def _fetch_peers_raw(self) -> Optional[list[dict[str, Any]]]:
        platform_url = (
            os.environ.get("PLATFORM_URL")
            or os.environ.get("MOLECULE_PLATFORM_URL", "")
        ).rstrip("/")
        workspace_token = os.environ.get("MOLECULE_WORKSPACE_TOKEN", "")
        if not platform_url or not workspace_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{platform_url}/registry/peers",
                    headers={
                        "Authorization": f"Bearer {workspace_token}",
                        "Origin": platform_url,
                    },
                )
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.debug("could not fetch peers from platform", exc_info=True)
            return None

    async def _dispatch_tool(self, tool_call: dict[str, Any]) -> str:
        name = (tool_call.get("function") or {}).get("name", "")
        if name == "list_peers":
            return await self._tool_list_peers()
        return f"Unknown tool: {name!r}"

    async def _tool_list_peers(self) -> str:
        blurb = await self._fetch_peers_blurb()
        return blurb or "No peers available (platform credentials not configured)."

    @staticmethod
    def _extract_assistant_text(data: dict[str, Any]) -> str:
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            logger.warning("Unexpected hermes-agent response shape: %r", data)
            return "(hermes-agent returned no content)"
