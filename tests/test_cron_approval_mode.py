"""``start.sh`` must make hermes' cron approval policy expressible — and must
keep DENYING by default.

Why this test exists
--------------------
On a cron-delivered turn hermes does not raise an approval and wait for a
human; it hard-denies. ``tools/approval.py`` (0.19.0)::

    mode = str(cfg_get(config, "approvals", "cron_mode", default="deny"))...
    if mode in {"approve", "off", "allow", "yes"}: return "approve"
    return "deny"

``start.sh`` never wrote ``cron_mode``, so that built-in ``deny`` applied to
every scheduled workspace the fleet has run. The agent receives "the user has
NOT consented... Do NOT retry" and completes the turn having executed nothing —
and because the turn never completes cleanly, the runtime lease goes idle and
the watchdog cancels and re-queues it every ~30s indefinitely, at WARNING,
inside the container. From outside the workspace is 1/1 with schedules armed
and simply quiet, which is indistinguishable from having no work to do.
Measured on prod tenant ``minori``, workspace ``c7937b21``, 2026-08-16
(molecule-core#5194).

What is actually asserted
-------------------------
This does not re-implement the shell logic and then test the re-implementation —
that would pass no matter what ``start.sh`` says. It slices the real block out
of the real file and executes it under ``sh``, so the assertions below fail if
someone edits ``start.sh``.

The default assertion is the load-bearing one. Permitting unattended code
execution is a security decision that belongs to whoever provisions a given
workspace, so it must stay opt-in; a change flipping the fleet-wide default to
``allow`` should break this test loudly rather than ship quietly.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

START_SH = pathlib.Path(__file__).resolve().parents[1] / "start.sh"

_BEGIN = '_CRON_MODE="${HERMES_CRON_APPROVAL_MODE:-deny}"'
_END = 'echo "  cron_mode:'

# The exact set hermes itself treats as "approve" (tools/approval.py). Keeping
# start.sh's validation list in step with this is the whole point of the
# warning branch: anything outside it fails closed to deny.
_ACCEPTED = ("approve", "off", "allow", "yes", "deny")


def _block() -> str:
    """Slice the cron_mode block verbatim out of start.sh."""
    lines = START_SH.read_text(encoding="utf-8").splitlines()
    try:
        first = next(i for i, ln in enumerate(lines) if _BEGIN in ln)
        last = next(i for i, ln in enumerate(lines) if _END in ln and i >= first)
    except StopIteration:  # pragma: no cover - the failure message is the point
        pytest.fail(
            "start.sh no longer contains the cron_mode block delimited by "
            f"{_BEGIN!r} .. {_END!r}. If it moved, update this test; if it was "
            "removed, scheduled workspaces are silently back to deny-always."
        )
    return "\n".join(lines[first : last + 1])


def _run(env_value: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    if env_value is not None:
        env["HERMES_CRON_APPROVAL_MODE"] = env_value
    return subprocess.run(
        ["sh", "-c", _block()],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_default_is_deny_when_env_is_unset():
    """Absent the env var, the emitted policy must match hermes' own default."""
    r = _run(None)
    assert r.returncode == 0, r.stderr
    assert 'cron_mode: "deny"' in r.stdout, (
        "start.sh must not widen the fleet-wide default. Unattended code "
        f"execution is opt-in per workspace. Got: {r.stdout!r}"
    )
    assert "WARNING" not in r.stderr


@pytest.mark.parametrize("mode", _ACCEPTED)
def test_every_value_hermes_accepts_passes_through_unwarned(mode: str):
    """The values hermes recognises must reach config.yaml verbatim."""
    r = _run(mode)
    assert r.returncode == 0, r.stderr
    assert f'cron_mode: "{mode}"' in r.stdout
    assert "WARNING" not in r.stderr, (
        f"{mode!r} is in hermes' accepted set but start.sh warned about it — "
        "the two lists have drifted apart."
    )


def test_opting_in_emits_allow():
    """The one thing an operator of an unattended agent actually needs."""
    assert 'cron_mode: "allow"' in _run("allow").stdout


def test_unrecognised_value_still_emits_but_warns_loudly():
    """A typo fails closed to deny inside hermes, which is invisible.

    ``approval.py`` compares against a fixed set, so ``alow`` is not an error —
    it is silently DENY, and the operator who set it believes they opted in.
    The value is still written verbatim (start.sh does not get to second-guess
    a policy key), but the boot log has to say so.
    """
    r = _run("alow")
    assert r.returncode == 0, r.stderr
    assert 'cron_mode: "alow"' in r.stdout
    assert "WARNING" in r.stderr
    assert "fails closed to DENY" in r.stderr


def test_the_block_is_reached_from_the_approvals_stanza():
    """A correct block emitted outside `approvals:` would configure nothing."""
    text = START_SH.read_text(encoding="utf-8")
    stanza = text.index('echo "approvals:"')
    assert stanza < text.index(_END), (
        "cron_mode is emitted before the approvals: header, so it would land "
        "under whatever mapping precedes it."
    )
