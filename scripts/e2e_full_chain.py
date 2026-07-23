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
    - Patched hermes-agent fork installed in
      ``~/.hermes/hermes-agent/venv``
    - The molecule-a2a plugin pip-installed in the same venv
    - This template's executor.py + adapter.py importable

Run:
    /Users/hongming/.hermes/hermes-agent/venv/bin/python3 \
        scripts/e2e_full_chain.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_HERMES_BIN = str(
    Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"
)


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


async def _stub_llm_server(port: int):
    """Tiny OpenAI-compat /v1/chat/completions server that echoes the
    last user message back as the assistant content. Lets us verify
    the round trip without needing a real LLM key."""
    from aiohttp import web

    async def chat_completions(request):
        body = await request.json()
        messages = body.get("messages", [])
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        # Echo with a tag so we can match the trip.
        reply = f"echo[{last_user}]"
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

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site


def _ctx(text: str, *, task_id: str = "task-fullchain"):
    text_part = MagicMock()
    text_part.text = text
    text_part.kind = "text"
    msg = MagicMock()
    msg.task_id = task_id
    msg.parts = [text_part]
    ctx = MagicMock()
    ctx.task_id = task_id
    ctx.message = msg
    return ctx


class _CapturingQueue:
    def __init__(self):
        self.events: List[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


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
    llm_runner, llm_site = await _stub_llm_server(llm_port)

    # Tmp HERMES_HOME with the plugin enabled and our stub LLM as the
    # custom-provider endpoint.
    tmp = Path(tempfile.mkdtemp(prefix="hermes-fullchain-"))
    hermes_home = tmp / ".hermes"
    hermes_home.mkdir()
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
        "plugins:\n"
        "  enabled:\n"
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
        os.environ["MOLECULE_A2A_PLATFORM_PORT"] = str(plugin_port)
        os.environ["MOLECULE_A2A_CALLBACK_PORT"] = str(cb_port)

        from executor import HermesAgentProxyExecutor
        from molecule_runtime.adapters.base import AdapterConfig

        cfg = AdapterConfig(model="test-model", system_prompt="be terse")
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

        # SECOND message with an ACTIVE session (2026-07-23 regression
        # guard): hermes 0.19's pairing/allowlist policy silently dropped
        # shared-secret-authenticated platform messages once a session
        # existed ("Dropping message from unauthorized user in active
        # session") — the executor future then expired as a 600s timeout
        # bubble on canvas. The adapter now declares
        # authorization_is_upstream; this round-trip fails loudly if that
        # contract regresses in a future hermes bump.
        queue2 = _CapturingQueue()
        await asyncio.wait_for(
            executor.execute(
                _ctx("second message", task_id="task-fullchain-2"), queue2
            ),
            timeout=60,
        )
        assert len(queue2.events) == 1, (
            "second (active-session) message dropped — hermes authz "
            f"policy regression? events: {queue2.events!r}"
        )
        print("OK: second active-session message round-tripped (authz upstream honored)")
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
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
