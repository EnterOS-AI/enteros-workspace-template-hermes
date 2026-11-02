"""hermes' own cron is off in every workspace (#400).

hermes ships a scheduler: the ``cronjob_manage`` tool (toolset ``cronjob``) lets
an agent create jobs that the gateway runs in fresh sessions, outside every
Molecule control (no /schedules entry, no turn cap, no drop-while-busy, and a
pause only suspends them). start.sh removes the toolset through
``agent.disabled_toolsets`` and pauses the jobs already stored on the volume
before the gateway starts. These tests run the real start.sh config block and
the real pause script rather than restating their logic.
"""
from __future__ import annotations

import datetime
import fcntl
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
START_SH = REPO / "start.sh"
PAUSE_SCRIPT = REPO / "scripts" / "pause-hermes-cron-jobs.py"
_BLOCK_RE = re.compile(r"^\{$\n(.*?)^\} >\"\$HERMES_CONFIG\"$", re.M | re.S)
NOW = datetime.datetime(2026, 11, 2, 21, 0, tzinfo=datetime.timezone.utc)


def _render(out: pathlib.Path, **env: str) -> tuple[dict, str]:
    """Run the config.yaml seed block with ``env``; return (parsed YAML, stderr)."""
    match = _BLOCK_RE.search(START_SH.read_text(encoding="utf-8"))
    assert match, "could not locate the config.yaml seed block in start.sh"
    block = match.group(0).replace('} >"$HERMES_CONFIG"', f'}} >"{out}"')
    script = "\n".join(["set -euo pipefail", 'DEFAULT_MODEL="m"', 'PROVIDER="custom"', block])
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "MOLECULE_"))}
    run_env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")])
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**run_env, **env},
        cwd=REPO,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return yaml.safe_load(out.read_text(encoding="utf-8")) or {}, proc.stderr


def _pause_module():
    spec = importlib.util.spec_from_file_location("pause_hermes_cron_jobs", PAUSE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job(job_id: str, **fields) -> dict:
    job = {
        "id": job_id,
        "name": f"job-{job_id}",
        "prompt": "run the relay",
        "schedule": {"kind": "interval", "minutes": 3, "display": "every 3m"},
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-11-02T21:03:00+00:00",
    }
    job.update(fields)
    return job


def _store(cron_dir: pathlib.Path, jobs: list, **top) -> pathlib.Path:
    cron_dir.mkdir(parents=True, exist_ok=True)
    path = cron_dir / "jobs.json"
    path.write_text(json.dumps({"jobs": jobs, "updated_at": "2026-10-30T00:17:04+00:00", **top}, indent=2), encoding="utf-8")
    return path


# --- the toolset ------------------------------------------------------------


def test_the_cronjob_toolset_is_disabled_by_default(tmp_path):
    cfg, stderr = _render(tmp_path / "config.yaml")
    assert cfg["agent"] == {"disabled_toolsets": ["cronjob"]}
    assert "HERMES_AGENT_CRON" not in stderr


@pytest.mark.parametrize("raw", ["allow", "ALLOW", "Allow"])
def test_allow_keeps_hermes_cron(tmp_path, raw):
    cfg, stderr = _render(tmp_path / "config.yaml", HERMES_AGENT_CRON=raw)
    assert "agent" not in cfg
    assert "HERMES_AGENT_CRON" not in stderr


@pytest.mark.parametrize("raw", ["yes", "on", "1", "true", "allowed", " allow"])
def test_any_other_value_keeps_it_off_and_says_so(tmp_path, raw):
    # Fail closed: a typo must not hand the agent an unsupervised scheduler.
    cfg, stderr = _render(tmp_path / "config.yaml", HERMES_AGENT_CRON=raw)
    assert cfg["agent"] == {"disabled_toolsets": ["cronjob"]}
    assert "HERMES_AGENT_CRON" in stderr and "stays off" in stderr, stderr


def test_start_sh_pauses_stored_jobs_before_the_gateway_starts():
    text = START_SH.read_text(encoding="utf-8")
    pause = text.index("pause-hermes-cron-jobs.py")
    launch = text.index('bash -c "cd /tmp && hermes gateway"')
    assert pause < launch, "stored jobs must be paused before the gateway (and its ticker) starts"
    guard = text.rfind("\nif ", 0, pause)
    assert '"${_AGENT_CRON:-off}" != allow' in text[guard:pause]


# --- the pause script --------------------------------------------------------


def test_runnable_jobs_are_paused_the_way_hermes_pause_job_does(tmp_path):
    mod = _pause_module()
    cron = tmp_path / "cron"
    already = _job("b", enabled=False, state="paused", paused_at="2026-10-31T00:00:00+00:00", paused_reason="by hand")
    done = _job("c", state="completed")
    broken = _job("d", state="error")
    disabled = _job("e", enabled=False, state="scheduled")
    path = _store(cron, [_job("a"), already, done, broken, disabled, "junk"], version=3)

    paused = mod.pause_all(str(cron), now=NOW)

    assert [j["id"] for j in paused] == ["a"]
    data = json.loads(path.read_text(encoding="utf-8"))
    first = data["jobs"][0]
    assert first["enabled"] is False
    assert first["state"] == "paused"
    assert first["paused_at"] == NOW.isoformat()
    assert first["paused_reason"] == mod.REASON
    assert first["schedule"] == {"kind": "interval", "minutes": 3, "display": "every 3m"}
    assert data["jobs"][1:] == [already, done, broken, disabled, "junk"]
    assert data["updated_at"] == NOW.isoformat()
    assert data["version"] == 3
    assert not mod._runnable(first)


def test_a_store_with_nothing_runnable_is_not_rewritten(tmp_path):
    mod = _pause_module()
    cron = tmp_path / "cron"
    path = _store(cron, [_job("b", enabled=False, state="paused", paused_at="2026-10-31T00:00:00+00:00")])
    before = path.read_bytes()
    assert mod.pause_all(str(cron), now=NOW) == []
    assert path.read_bytes() == before


def test_no_store_is_a_no_op(tmp_path):
    mod = _pause_module()
    cron = tmp_path / "cron"
    cron.mkdir()
    assert mod.pause_all(str(cron), now=NOW) == []
    assert not (cron / "jobs.json").exists()


def test_an_unreadable_store_is_left_alone(tmp_path, capsys):
    mod = _pause_module()
    cron = tmp_path / "cron"
    cron.mkdir()
    path = cron / "jobs.json"
    path.write_text("{not json", encoding="utf-8")
    assert mod.pause_all(str(cron), now=NOW) == []
    assert path.read_text(encoding="utf-8") == "{not json"
    assert "jobs NOT paused" in capsys.readouterr().out


def test_a_held_lock_is_not_waited_out_forever(tmp_path, monkeypatch, capsys):
    mod = _pause_module()
    monkeypatch.setattr(mod, "LOCK_TIMEOUT_SECONDS", 0.2)
    cron = tmp_path / "cron"
    path = _store(cron, [_job("a")])
    before = path.read_bytes()
    with open(cron / ".jobs.lock", "a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert mod.pause_all(str(cron), now=NOW) == []
    assert path.read_bytes() == before
    assert "jobs NOT paused" in capsys.readouterr().out


def test_the_script_logs_each_paused_job_and_always_exits_zero(tmp_path):
    cron = tmp_path / "cron"
    _store(cron, [_job("a1"), _job("a2", name="zernio-relay")])
    proc = subprocess.run(
        [sys.executable, "-I", str(PAUSE_SCRIPT), str(cron)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if "paused job" in ln]
    assert lines == [
        "[start.sh] hermes cron: paused job a1 (job-a1, every 3m)",
        "[start.sh] hermes cron: paused job a2 (zernio-relay, every 3m)",
    ]
    # A second boot finds nothing left to pause.
    again = subprocess.run(
        [sys.executable, "-I", str(PAUSE_SCRIPT), str(cron)], capture_output=True, text=True, check=False
    )
    assert again.returncode == 0 and "paused job" not in again.stdout


def test_bad_arguments_do_not_fail_the_boot():
    proc = subprocess.run([sys.executable, "-I", str(PAUSE_SCRIPT)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "usage" in proc.stdout
