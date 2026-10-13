"""Feed the Molecule runtime's turn lease from hermes tool calls (runtime#417).

The runtime judges whether a turn is still working from the mtime of
``MOLECULE_TOOL_ACTIVITY_FILE`` (molecule_runtime.turn_lease, feeder C). The
hermes gateway runs every tool call, but it is started once by start.sh, so
the runtime's per-turn export of that variable never reached it. start.sh
now exports one path to both processes at boot, and this hook touches it
after every tool call.

``post_tool_call`` is an observer: hermes skips a callback that is slow or
raises and the tool result is unaffected. ``pre_tool_call`` is deliberately
not used. It is a policy hook, so a callback that raises or times out there
blocks the tool call.
"""
from __future__ import annotations

import os

ACTIVITY_FILE_ENV = "MOLECULE_TOOL_ACTIVITY_FILE"


def touch_activity_file(**_payload) -> None:
    """Advance the activity file's mtime. Never raises."""
    path = os.environ.get(ACTIVITY_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        os.utime(path, None)
    except FileNotFoundError:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError:
            pass
    except OSError:
        pass
    return None


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", touch_activity_file)
