"""start.sh in a Light run pod (MOLECULE_RUN_MODE=once, contract C1).

A Light run pod boots this image on a private copy of a parked agent's volume,
runs one scheduled prompt through molecule-runtime's once mode and exits. These
tests pin what start.sh does differently there, and that a normal boot does
nothing differently. They run real blocks out of start.sh under bash, with stubs
standing in for the processes it launches.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Optional

import pytest
import yaml
from packaging.version import Version

from molecule_runtime.run_once import boot_gate
from molecule_runtime.workspace_mode import UnknownRunMode, resolve_run_mode

ROOT = pathlib.Path(__file__).resolve().parents[1]
START_SH = ROOT / "start.sh"
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"
BLOCK_START = "# --- Light run pod"
BLOCK_END = "# --- end Light run pod ---"

posix_only = pytest.mark.skipif(os.name != "posix", reason="runs start.sh blocks under bash")


def _conformance_script() -> str:
    """The pytest command of the required `conformance` job."""
    steps = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["conformance"]["steps"]
    (script,) = [
        step.get("run", "")
        for step in steps
        if step.get("name") == "Run adapter and private-runtime contracts"
    ]
    return script


def test_runtime_pin_has_once_mode():
    # molecule-runtime 0.4.95 is the first release with once mode, the copy step
    # (molecule-runtime-light-copy) and the run-pod silence this image relies on.
    pin = (ROOT / ".runtime-version").read_text(encoding="utf-8").strip()
    assert Version(pin) >= Version("0.4.95"), pin


def test_this_file_runs_in_the_required_gate():
    assert "tests/test_start_sh_run_mode.py" in _conformance_script().split()


# --- the run-mode block ---------------------------------------------------------


def _start_sh_lines() -> list[str]:
    return START_SH.read_text(encoding="utf-8").splitlines()


def _block() -> str:
    """The run-mode block, markers included; exactly one of each marker."""
    lines = _start_sh_lines()
    starts = [i for i, line in enumerate(lines) if line.startswith(BLOCK_START)]
    ends = [i for i, line in enumerate(lines) if line == BLOCK_END]
    assert len(starts) == 1 and len(ends) == 1, "start.sh has no single Light run pod block"
    assert starts[0] < ends[0]
    return "\n".join(lines[starts[0] : ends[0] + 1]) + "\n"


def _clean_env(**env: Optional[str]) -> dict:
    """The test's environment without any MOLECULE_* / HERMES_* variable, plus
    ``env`` (None leaves a variable out)."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("MOLECULE_", "HERMES_"))}
    base.update({k: v for k, v in env.items() if v is not None})
    return base


@dataclass
class BlockRun:
    rc: int
    stdout: str
    stderr: str
    run_once: Optional[str]
    reply: Optional[str]
    max: Optional[str]


def _run_block(tmp_path: pathlib.Path, **env: Optional[str]) -> BlockRun:
    """Run the block under ``set -euo pipefail`` and read back what it left for
    the rest of the boot: RUN_ONCE, and the two bounds as a child process sees
    them (printenv, so only exported values count)."""
    probe = tmp_path / "probe"
    script = "\n".join([
        "set -euo pipefail",
        _block(),
        f'{{ printf "%s\\n" "$RUN_ONCE"; printenv HERMES_TURN_REPLY_DEADLINE_SECONDS || echo "<unset>";'
        f' printenv HERMES_TURN_MAX_SECONDS || echo "<unset>"; }} >"{probe}"',
    ])
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=_clean_env(**env), cwd=ROOT, check=False,
    )
    values = probe.read_text(encoding="utf-8").splitlines() if probe.exists() else [None, None, None]
    return BlockRun(proc.returncode, proc.stdout, proc.stderr, *values)


def _run_env(**over: Optional[str]) -> dict:
    """A complete once-mode environment for the runtime's boot gate."""
    env = {
        "MOLECULE_RUN_MODE": "once",
        "MOLECULE_RUN_PROMPT": "write the weekly report",
        "MOLECULE_RUN_SCHEDULE": "daily-report",
        "MOLECULE_RUN_ID": "light-ws-abc-123",
        "WORKSPACE_ID": "11111111-2222-3333-4444-555555555555",
        "PLATFORM_URL": "http://platform.test",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


@posix_only
@pytest.mark.parametrize(
    "raw",
    [None, "", " ", "\t", "\n", "once", "ONCE", " Once ", "\tonce\n",
     "twice", "serve", "1", "true", "on ce", "onc", "once1"],
)
def test_run_mode_agrees_with_the_runtime(tmp_path, raw):
    try:
        want = resolve_run_mode({} if raw is None else {"MOLECULE_RUN_MODE": raw})
    except UnknownRunMode:
        want = "refused"
    r = _run_block(tmp_path, MOLECULE_RUN_MODE=raw)
    if want == "refused":
        assert r.rc == 2 and r.run_once is None and r.stdout == ""
        assert len(r.stderr.splitlines()) == 1 and "MOLECULE_RUN_MODE" in r.stderr, r.stderr
    elif want == "serve":
        assert (r.rc, r.run_once, r.stdout, r.stderr) == (0, "0", "", "")
    else:
        assert (r.rc, r.run_once, r.stderr) == (0, "1", "")
        assert r.stdout.startswith("[start.sh] MOLECULE_RUN_MODE=once: Light run pod")
        assert len(r.stdout.splitlines()) == 1


@posix_only
def test_a_normal_boot_sets_nothing(tmp_path):
    # An agent-level knob stays exactly as the agent's env has it.
    r = _run_block(tmp_path, HERMES_TURN_REPLY_DEADLINE_SECONDS="1500")
    assert (r.rc, r.run_once, r.reply, r.max, r.stdout, r.stderr) == (0, "0", "1500", "<unset>", "", "")


@posix_only
@pytest.mark.parametrize("raw", [None, "", "  ", "60", " 600 ", "+600", "0600", "7200"])
def test_once_mode_bounds_the_turn_by_the_run_deadline(tmp_path, raw):
    # The executor parses both with float(), which reads every value the runtime's
    # int() accepts as the same number; anything else, the runtime refuses at boot.
    want = boot_gate(
        _run_env(MOLECULE_RUN_DEADLINE_SECONDS=raw), prepare_only=False, started_at=0.0, started_monotonic=0.0,
    ).deadline_seconds
    r = _run_block(tmp_path, MOLECULE_RUN_MODE="once", MOLECULE_RUN_DEADLINE_SECONDS=raw)
    assert r.rc == 0 and r.reply == r.max
    assert float(r.reply) == want
    assert f"= {r.reply}s" in r.stdout


@posix_only
def test_once_mode_overrides_the_agents_own_bounds(tmp_path):
    # Bounds tuned for chat callers must not end a scheduled run early.
    r = _run_block(
        tmp_path, MOLECULE_RUN_MODE="once", MOLECULE_RUN_DEADLINE_SECONDS="600",
        HERMES_TURN_REPLY_DEADLINE_SECONDS="1500", HERMES_TURN_MAX_SECONDS="0",
    )
    assert (r.rc, r.reply, r.max) == (0, "600", "600")


def test_the_mode_is_decided_before_the_first_boot_step():
    code = [(i, line) for i, line in enumerate(_start_sh_lines()) if not line.lstrip().startswith("#")]
    smoke_exec = next(i for i, line in code if "molecule-runtime" in line and line.lstrip().startswith("exec"))
    block = next(i for i, line in enumerate(_start_sh_lines()) if line.startswith(BLOCK_START))
    first_step = next(i for i, line in code if i > smoke_exec and ("chown" in line or "install " in line))
    assert smoke_exec < block < first_step, "decide the run mode after the smoke branch, before any boot step"
