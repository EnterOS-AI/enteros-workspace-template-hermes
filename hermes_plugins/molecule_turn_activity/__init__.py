"""Feed the Molecule runtime's turn lease from hermes tool calls (runtime#417).

The runtime judges whether a turn is still working from the mtime of
``MOLECULE_TOOL_ACTIVITY_FILE`` (molecule_runtime.turn_lease, feeder C). The
hermes gateway runs every tool call, but it is started once by start.sh, so
the runtime's per-turn export of that variable never reached it. start.sh
now exports one path to both processes at boot, and this hook rewrites it
after every tool call.

The file's content is the latest tool event as one JSON object (``v``,
``boot``, ``seq``, ``tool``, ``status``, ``args``, ``duration_ms``, ``ts``).
The executor turns each new ``seq`` into the platform's ``agent_log`` activity
row: the canvas tool chip, and the activity the platform's stall watchdog
needs to see while a long turn is marked busy. Readers that only compare the
mtime are unaffected. ``boot`` changes when the gateway restarts, which resets
``seq``.

``post_tool_call`` is an observer: hermes skips a callback that is slow or
raises and the tool result is unaffected. ``pre_tool_call`` is deliberately
not used. It is a policy hook, so a callback that raises or times out there
blocks the tool call.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

ACTIVITY_FILE_ENV = "MOLECULE_TOOL_ACTIVITY_FILE"
RECORD_VERSION = 1
# The executor shows at most a short preview; this only bounds the file.
_ARGS_MAX_CHARS = 400

_BOOT = uuid.uuid4().hex[:16]
_SEQ_LOCK = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq
    with _SEQ_LOCK:
        _seq += 1
        return _seq


def _args_text(args) -> str:
    if args is None:
        return ""
    try:
        text = args if isinstance(args, str) else json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    return text[:_ARGS_MAX_CHARS]


def _record(payload: dict) -> dict:
    duration = payload.get("duration_ms")
    return {
        "v": RECORD_VERSION,
        "boot": _BOOT,
        "seq": _next_seq(),
        "tool": str(payload.get("tool_name") or ""),
        "status": str(payload.get("status") or "ok"),
        "args": _args_text(payload.get("args")),
        "duration_ms": duration if isinstance(duration, int) else 0,
        "ts": time.time(),
    }


def _write_atomically(path: str, text: str) -> None:
    # Hermes runs parallel tool calls on threads: each writer gets its own temp
    # file, and os.replace makes the last one win whole, never interleaved.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _touch(path: str) -> None:
    try:
        os.utime(path, None)
    except FileNotFoundError:
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


def touch_activity_file(**payload) -> None:
    """Record this tool call and advance the activity file's mtime. Never raises."""
    path = os.environ.get(ACTIVITY_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        _write_atomically(path, json.dumps(_record(payload), ensure_ascii=False))
        return None
    except Exception:
        pass
    # The record is telemetry; the mtime is the liveness signal. Keep the
    # latter even when the former cannot be written.
    try:
        _touch(path)
    except Exception:
        pass
    return None


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", touch_activity_file)
