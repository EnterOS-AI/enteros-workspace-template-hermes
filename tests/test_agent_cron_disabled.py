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


def _assert_paused(mod, job: dict) -> None:
    assert job["enabled"] is False
    assert job["state"] == "paused"
    assert job["paused_at"] == NOW.isoformat()
    assert job["paused_reason"] == mod.REASON
    assert not mod._runnable(job)


def test_runnable_jobs_are_paused_the_way_hermes_pause_job_does(tmp_path):
    mod = _pause_module()
    cron = tmp_path / "cron"
    already = _job("b", enabled=False, state="paused", paused_at="2026-10-31T00:00:00+00:00", paused_reason="by hand")
    done = _job("c", state="completed")
    disabled = _job("e", enabled=False, state="scheduled")
    once_error = _job("f", state="error", schedule={"kind": "once", "run_at": "2026-10-30T00:00:00+00:00", "display": "once"})
    path = _store(cron, [_job("a"), already, done, disabled, once_error, "junk"], version=3)

    paused = mod.pause_all(str(cron), now=NOW)

    assert [job_id for job_id, _ in paused] == ["a"]
    data = json.loads(path.read_text(encoding="utf-8"))
    first = data["jobs"][0]
    _assert_paused(mod, first)
    assert first["schedule"] == {"kind": "interval", "minutes": 3, "display": "every 3m"}
    assert first["next_run_at"] == "2026-11-02T21:03:00+00:00"
    assert data["jobs"][1:] == [already, done, disabled, once_error, "junk"]
    assert data["updated_at"] == NOW.isoformat()
    assert data["version"] == 3


@pytest.mark.parametrize("kind", ["cron", "interval"])
def test_a_recurring_job_in_error_is_paused_too(tmp_path, kind):
    # hermes marks a recurring job `error` when it cannot compute the next run, leaves it enabled,
    # and its due scan re-arms it to `scheduled` (cron/jobs.py _recover_missing_next_run): not final.
    mod = _pause_module()
    cron = tmp_path / "cron"
    path = _store(cron, [_job("d", state="error", schedule={"kind": kind, "expr": "*/3 * * * *", "display": "*/3"})])
    assert [job_id for job_id, _ in mod.pause_all(str(cron), now=NOW)] == ["d"]
    _assert_paused(mod, json.loads(path.read_text(encoding="utf-8"))["jobs"][0])


def test_an_id_keyed_store_is_paused_in_place(tmp_path):
    # hermes flattens {"jobs": {id: job}} on load (cron/jobs.py load_jobs); the key is the id.
    mod = _pause_module()
    cron = tmp_path / "cron"
    keyed = _job("x")
    del keyed["id"]
    path = _store(cron, {"k1": keyed, "k2": _job("k2", state="completed"), "k3": "junk"})
    paused = mod.pause_all(str(cron), now=NOW)
    assert [job_id for job_id, _ in paused] == ["k1"]
    data = json.loads(path.read_text(encoding="utf-8"))
    _assert_paused(mod, data["jobs"]["k1"])
    assert data["jobs"]["k2"]["state"] == "completed" and data["jobs"]["k3"] == "junk"


def test_a_bare_list_store_is_paused_in_place(tmp_path):
    # hermes wraps a bare list on load (cron/jobs.py load_jobs).
    mod = _pause_module()
    cron = tmp_path / "cron"
    cron.mkdir()
    path = cron / "jobs.json"
    path.write_text(json.dumps([_job("a")]), encoding="utf-8")
    assert [job_id for job_id, _ in mod.pause_all(str(cron), now=NOW)] == ["a"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    _assert_paused(mod, data[0])


@pytest.mark.parametrize("payload", ['{"jobs": "x"}', "42", '"jobs"'])
def test_a_store_without_a_job_list_is_reported_and_left_alone(tmp_path, capsys, payload):
    mod = _pause_module()
    cron = tmp_path / "cron"
    cron.mkdir()
    path = cron / "jobs.json"
    path.write_text(payload, encoding="utf-8")
    assert mod.pause_all(str(cron), now=NOW) == []
    assert path.read_text(encoding="utf-8") == payload
    assert "jobs NOT paused" in capsys.readouterr().out


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
