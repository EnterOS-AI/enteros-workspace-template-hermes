"""A2A → hermes-agent HTTP bridge.

A thin proxy: take each incoming A2A message, forward it to
`POST /v1/chat/completions` on the in-container hermes-agent API server
(default 127.0.0.1:8642), and emit the assistant response on the A2A
event queue. That's it — hermes-agent owns tool selection, memory,
skills, provider routing, streaming, and everything else that used to
live in this file.

Key resolution
--------------
- Base URL from env ``HERMES_API_BASE`` (default ``http://127.0.0.1:8642/v1``)
- Bearer token from env ``API_SERVER_KEY`` — injected by start.sh from a
  per-container random value. The platform never sees this token.
- Model from ``AdapterConfig.model`` — passed verbatim. hermes-agent
  accepts any string its resolver understands; the canvas Config tab
  constrains choices to the list in ``config.yaml``.

Streaming is intentionally disabled in this first bridge revision — the
A2A response envelope is simpler to construct from a final message and
the extra latency is negligible for typical agent turns. The design doc
(docs/ARCHITECTURE.md) covers the upgrade path once streaming is
needed.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message

from molecule_runtime.adapters.base import AdapterConfig
from molecule_runtime.executor_helpers import extract_message_text

logger = logging.getLogger(__name__)


_DEFAULT_BASE = "http://127.0.0.1:8642/v1"
# hermes-agent sessions can run long when tool-using; bridge timeout is
# generous but finite so a hung gateway doesn't wedge the A2A queue.
_REQUEST_TIMEOUT = 600.0


class HermesAgentProxyExecutor(AgentExecutor):
    """Forwards every A2A turn to hermes-agent's OpenAI-compat endpoint."""

    def __init__(self, config: AdapterConfig):
        self._config = config
        self._base = os.environ.get("HERMES_API_BASE", _DEFAULT_BASE).rstrip("/")
        # API key is read lazily per-request so start.sh can rotate it
        # without restarting molecule-runtime.

    # ------------------------------------------------------------------
    # AgentExecutor contract
    # ------------------------------------------------------------------
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        prompt = extract_message_text(context.message) or ""
        if not prompt.strip():
            await event_queue.enqueue_event(
                new_agent_text_message("(empty prompt — nothing to do)")
            )
            return

        payload = self._build_payload(prompt)
        headers = self._build_headers()

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            logger.error("hermes-agent %s: %s", exc.response.status_code, body)
            await event_queue.enqueue_event(
                new_agent_text_message(
                    f"[hermes-agent error {exc.response.status_code}] {body}"
                )
            )
            return
        except httpx.RequestError as exc:
            logger.exception("hermes-agent transport error")
            await event_queue.enqueue_event(
                new_agent_text_message(f"[hermes-agent unreachable] {exc!s}")
            )
            return

        text = self._extract_assistant_text(data)
        await event_queue.enqueue_event(new_agent_text_message(text))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # hermes-agent doesn't expose a per-request cancel over HTTP in the
        # current API surface. Dropping the httpx client on timeout is the
        # best we can do today — the server-side run will complete and be
        # discarded. Revisit when hermes adds DELETE /v1/runs/{id} for
        # chat-completions paths (already exists for the /v1/runs path).
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_payload(self, user_text: str) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if self._config.system_prompt:
            messages.append({"role": "system", "content": self._config.system_prompt})
        messages.append({"role": "user", "content": user_text})

        payload: dict[str, Any] = {
            # hermes-agent's api_server adapter accepts any model string the
            # gateway is configured for. Empty → server-side default.
            "model": self._config.model or "hermes-agent",
            "messages": messages,
            "stream": False,
        }
        return payload

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("API_SERVER_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _extract_assistant_text(data: dict[str, Any]) -> str:
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            logger.warning("Unexpected hermes-agent response shape: %r", data)
            return "(hermes-agent returned no content)"
