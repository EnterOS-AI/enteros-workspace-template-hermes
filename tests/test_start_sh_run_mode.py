"""start.sh in a Light run pod (MOLECULE_RUN_MODE=once, contract C1).

A Light run pod boots this image on a private copy of a parked agent's volume,
runs one scheduled prompt through molecule-runtime's once mode and exits. These
tests pin what start.sh does differently there, and that a normal boot does
nothing differently. They run real blocks out of start.sh under bash, with stubs
standing in for the processes it launches.
"""
from __future__ import annotations

import pathlib

import yaml
from packaging.version import Version

ROOT = pathlib.Path(__file__).resolve().parents[1]
START_SH = ROOT / "start.sh"
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"


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
