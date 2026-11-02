#!/usr/bin/env python3
"""Pause the hermes cron jobs already stored on this workspace's volume (#400).

start.sh removes hermes' ``cronjob`` toolset, so an agent can no longer create
jobs, but the ones it created earlier keep firing: the gateway's ticker runs
whatever ``<HERMES_HOME>/cron/jobs.json`` holds, and that file lives on the
persistent /configs volume. Each runnable job is marked paused the way hermes'
own ``cron.jobs.pause_job`` does it (enabled=false, state=paused, paused_at,
paused_reason), under hermes' ``.jobs.lock`` flock, and one line per job goes
to the boot log so it says what stopped.

Usage: pause-hermes-cron-jobs.py <cron dir>. Always exits 0: a problem here is
reported and must never block the boot.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import sys
import tempfile
import time

REASON = (
    "Molecule workspaces schedule work through the platform scheduler; hermes' "
    "own cron is turned off (HERMES_AGENT_CRON=allow turns it back on)."
)
# hermes' own writers poll the same lock for up to 30 s; at boot nothing should
# hold it, so a short wait is enough and keeps a wedged holder from stalling boot.
LOCK_TIMEOUT_SECONDS = 10.0
# hermes keeps these states whatever `enabled` says (cron/jobs.py
# effective_job_state); they never fire again, so there is nothing to pause.
_TERMINAL_STATES = {"completed", "error"}


def _log(message: str) -> None:
    print(f"[start.sh] hermes cron: {message}", flush=True)


def _runnable(job: dict) -> bool:
    """hermes' is_job_runnable (enabled and no pause marker), minus terminal jobs."""
    if not job.get("enabled", True):
        return False
    state = str(job.get("state") or "").strip()
    if state == "paused" or job.get("paused_at"):
        return False
    return state not in _TERMINAL_STATES


def _acquire_lock(cron_dir: str):
    lock = open(os.path.join(cron_dir, ".jobs.lock"), "a+")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except OSError:
            if time.monotonic() >= deadline:
                lock.close()
                return None
            time.sleep(0.1)


def _write_atomically(path: str, data: dict) -> None:
    # The volume is what makes these jobs survive restarts, so a torn write here
    # would lose every job: write beside, fsync, rename, fsync the directory.
    target = os.path.realpath(path)
    directory = os.path.dirname(target)
    st = os.stat(target)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".jobs_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def pause_all(cron_dir: str, now: datetime.datetime | None = None) -> list[dict]:
    """Pause every runnable job in ``<cron_dir>/jobs.json``; return the jobs paused."""
    jobs_file = os.path.join(cron_dir, "jobs.json")
    if not os.path.isfile(jobs_file):
        return []
    lock = _acquire_lock(cron_dir)
    if lock is None:
        _log(f"{jobs_file} stayed locked for {LOCK_TIMEOUT_SECONDS:.0f}s; jobs NOT paused")
        return []
    try:
        try:
            with open(jobs_file, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            _log(f"cannot read {jobs_file} ({exc}); jobs NOT paused")
            return []
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return []
        stamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat()
        paused = []
        for job in jobs:
            if isinstance(job, dict) and _runnable(job):
                job.update(enabled=False, state="paused", paused_at=stamp, paused_reason=REASON)
                paused.append(job)
        if paused:
            data["updated_at"] = stamp
            _write_atomically(jobs_file, data)
        return paused
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def _describe(job: dict) -> str:
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    when = job.get("schedule_display") or schedule.get("display") or schedule.get("expr") or schedule.get("kind") or "?"
    return f"{job.get('id', '?')} ({job.get('name') or 'unnamed'}, {when})"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _log("usage: pause-hermes-cron-jobs.py <cron dir>")
        return 0
    try:
        paused = pause_all(argv[1])
    except Exception as exc:  # noqa: BLE001 - never fail the boot
        _log(f"pausing failed ({type(exc).__name__}: {exc}); jobs NOT paused")
        return 0
    for job in paused:
        _log(f"paused job {_describe(job)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
