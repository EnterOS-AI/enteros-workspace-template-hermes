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
import json
import os
import shutil
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
        marker_at = max(
            (i for i, m in enumerate(messages)
             if m.get("role") == "user" and TOOL_MARKER in _text(m)),
            default=-1,
        )
        if marker_at >= 0 and body.get("tools"):
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
            calls = [{
                "index": 0, "id": "call_e2e_1", "type": "function",
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
    msg.message = None  # else extract_message_text() returns "" and execute() never dispatches
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
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
