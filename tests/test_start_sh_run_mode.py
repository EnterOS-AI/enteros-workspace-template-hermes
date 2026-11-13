"""start.sh in a Light run pod (MOLECULE_RUN_MODE=once, contract C1).

A Light run pod boots this image on a private copy of a parked agent's volume,
runs one scheduled prompt through molecule-runtime's once mode and exits. These
tests pin what start.sh does differently there, and that a normal boot does
nothing differently. They run real blocks out of start.sh under bash, with stubs
standing in for the processes it launches.
"""
from __future__ import annotations

import collections
import json
import os
import pathlib
import re
import subprocess
import sys
import time
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
     "twice", "serve", "1", "true", "on ce", "onc", "once1",
     "on\nce", "once\nonce", "tw\rice"],
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
@pytest.mark.parametrize(
    "raw,shown",
    [
        ("on\nce", "on\\nce"),
        ("once\nrm -rf /", "once\\nrm -rf /"),
        ("tw\r\nice", "tw\\r\\nice"),
        ("a\tb\x1bc", "a\\tb?c"),
        ("x" * 39 + "\n" + "y" * 60, "x" * 39 + "\\n"),  # at most 40 characters of it
    ],
)
def test_a_refused_value_is_shown_on_one_line(tmp_path, raw, shown):
    # A value can hold any byte: the refusal names it with its control
    # characters escaped, as molecule-runtime's own refusal does, so one log
    # line stays one line, and it shows at most 40 characters of the value.
    r = _run_block(tmp_path, MOLECULE_RUN_MODE=raw)
    assert (r.rc, r.stdout) == (2, "")
    assert r.stderr == (
        f"[start.sh] MOLECULE_RUN_MODE='{shown}' is not a run mode this image knows "
        "(unset or 'once'); refusing to boot\n"
    )


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


# --- the generated config.yaml ------------------------------------------------------

_SEED_RE = re.compile(r"^\{$\n(.*?)^\} >\"\$HERMES_CONFIG\"$", re.M | re.S)


def _render_config(out_dir: pathlib.Path, **env: Optional[str]) -> dict:
    """The run-mode block, then start.sh's config.yaml seed block, under bash
    with ``env``; the config they render, parsed."""
    match = _SEED_RE.search(START_SH.read_text(encoding="utf-8"))
    assert match, "could not locate the config.yaml seed block in start.sh"
    out_dir.mkdir()
    out = out_dir / "config.yaml"
    seed = match.group(0).replace('} >"$HERMES_CONFIG"', f'}} >"{out}"')
    script = "\n".join(["set -euo pipefail", _block(), 'DEFAULT_MODEL="m"', 'PROVIDER="custom"', seed])
    run_env = _clean_env(**env)
    # The seed block runs python3 + PyYAML: this interpreter first on PATH.
    run_env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), run_env.get("PATH", "")])
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=run_env, cwd=ROOT, check=False)
    assert proc.returncode == 0, proc.stderr
    return yaml.safe_load(out.read_text(encoding="utf-8")) or {}


@posix_only
def test_a_run_turns_off_hermes_background_review_and_nothing_else(tmp_path):
    # Review 21723 O1. hermes' background memory/skill review is a full agent on
    # the turn's session; its model calls fire the hooks that write the run's
    # model-call record, so it could overwrite the turn's verdict before the
    # executor reads it. hermes at HERMES_COMMIT reads this key before every
    # automatic review (agent/background_review.py, load_background_review_settings).
    serve = _render_config(tmp_path / "serve")
    run = _render_config(tmp_path / "run", MOLECULE_RUN_MODE="once")
    assert run["auxiliary"] == {"background_review": {"enabled": False}}
    assert "auxiliary" not in serve  # a workspace keeps its review
    assert {k: v for k, v in run.items() if k != "auxiliary"} == serve


def test_the_mode_is_decided_before_the_first_boot_step():
    code = [(i, line) for i, line in enumerate(_start_sh_lines()) if not line.lstrip().startswith("#")]
    smoke_exec = next(i for i, line in code if "molecule-runtime" in line and line.lstrip().startswith("exec"))
    block = next(i for i, line in enumerate(_start_sh_lines()) if line.startswith(BLOCK_START))
    first_step = next(i for i, line in code if i > smoke_exec and ("chown" in line or "install " in line))
    assert smoke_exec < block < first_step, "decide the run mode after the smoke branch, before any boot step"


# --- the boot from the MCP server to molecule-runtime ---------------------------
#
# The tail of start.sh runs for real under bash: from the Molecule MCP server's
# launch to the exec of molecule-runtime. Stubs on PATH stand in for every
# process it starts and record their name, arguments and environment; the
# reconcile watcher's absolute path is pointed at a stub too. A preamble supplies
# what the earlier part of start.sh would have set, and the real run-mode block.

TAIL_START = "# --- Start the Molecule A2A MCP HTTP server ---"
WATCHER_PATH = "/usr/local/bin/mcp-reconcile-watch.sh"
GATEWAY_PATH_SEED = 'GATEWAY_PATH="/home/agent/.local/bin:'
RECORDED_ENV = (
    "HOME", "CONFIGS_DIR", "MOLECULE_RUNTIME_PREPARE_ONLY", "MOLECULE_RUN_MODE",
    "HERMES_TURN_REPLY_DEADLINE_SECONDS", "HERMES_TURN_MAX_SECONDS", "HERMES_GATEWAY_BUSY_INPUT_MODE",
)

# One recorder behind every stub: argv[1] names the stub.
_RECORDER = '''
import json, os, sys
name, argv = sys.argv[1], sys.argv[2:]
if name == "python3":
    if argv[:2] != ["-m", "molecule_runtime.a2a_mcp_server"]:
        os.execv({python!r}, [{python!r}, *argv])
    name = "mcp-server"
elif name == "hermes":
    name = {{("gateway",): "gateway", ("mcp", "list"): "mcp-list"}}.get(tuple(argv), "hermes " + " ".join(argv))
elif name in ("curl", "install"):
    if name == "install" and argv and not argv[-1].startswith("-"):
        if "-d" in argv:
            os.makedirs(argv[-1], exist_ok=True)
        else:
            open(argv[-1], "a").close()
    sys.exit(0)
env = {{k: os.environ[k] for k in {recorded!r} if k in os.environ}}
env.update({{k: v for k, v in os.environ.items() if k.startswith("MCPWATCH_")}})
if name == "gateway":
    env["PATH"] = os.environ.get("PATH", "")
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"name": name, "argv": argv, "env": env}}) + "\\n")
if name == "mcp-list":
    print("  molecule      http://127.0.0.1:9100/mcp   enabled")
if name == "prepare":
    sys.exit({prepare_rc})
'''


@dataclass
class Boot:
    rc: int
    stdout: str
    stderr: str
    records: list
    gateway_path: str

    def cmds(self) -> list:
        return [r["name"] for r in self.records]

    def env(self, name: str) -> dict:
        (record,) = [r for r in self.records if r["name"] == name]
        return record["env"]


def _tail() -> str:
    text = START_SH.read_text(encoding="utf-8")
    assert text.count(TAIL_START) == 1
    return text[text.index(TAIL_START):]


def _boot_tail(
    tmp_path: pathlib.Path, *, mode: Optional[str] = None, deadline: Optional[str] = None,
    prepare_rc: int = 0, prepare: bool = True,
) -> Boot:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "records.jsonl"
    recorder = stubs / "record.py"
    recorder.write_text(
        _RECORDER.format(python=sys.executable, recorded=RECORDED_ENV, log=str(log), prepare_rc=prepare_rc),
        encoding="utf-8",
    )
    names = {"python3": "python3", "curl": "curl", "install": "install", "hermes": "hermes",
             "molecule-runtime": "runtime", "watcher.sh": "watcher"}
    if prepare:
        names["molecule-runtime-prepare"] = "prepare"
    for file_name, stub_name in names.items():
        stub = stubs / file_name
        stub.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{recorder}" {stub_name} "$@"\n', encoding="utf-8")
        stub.chmod(0o755)

    tail = _tail()
    assert tail.count(WATCHER_PATH) == 1 and tail.count(GATEWAY_PATH_SEED) == 1
    tail = tail.replace(WATCHER_PATH, str(stubs / "watcher.sh"))
    tail = tail.replace(GATEWAY_PATH_SEED, f'GATEWAY_PATH="{stubs}:/home/agent/.local/bin:')
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    preamble = "\n".join([
        "set -euo pipefail",
        f'HERMES_HOME="{hermes_home}"',
        'HERMES_CONFIG="$HERMES_HOME/config.yaml"',
        'LOG_FILE="$HERMES_HOME/gateway.log"',
        ': >"$LOG_FILE"',
        'AS_AGENT=""',
        'HERMES_AGENT_PYTHON=""',
        ". scripts/process-liveness.sh",
        "mirror_log_to_stdout() { :; }",
    ])
    block = _block() if BLOCK_START in START_SH.read_text(encoding="utf-8") else ""
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("MOLECULE_", "HERMES_", "API_SERVER_", "MCPWATCH_")) and k not in RECORDED_ENV
    }
    env["PATH"] = f"{stubs}:/usr/bin:/bin"
    if mode is not None:
        env["MOLECULE_RUN_MODE"] = mode
    if deadline is not None:
        env["MOLECULE_RUN_DEADLINE_SECONDS"] = deadline
    proc = subprocess.run(
        ["bash", "-c", "\n".join([preamble, block, tail])],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120, check=False,
    )
    # The MCP server, the gateway and the watcher are started in the background:
    # give their records a moment to land, then a little more for anything late.
    deadline_at = time.monotonic() + 5
    want = {"mcp-server", "gateway"} if proc.returncode == 0 else set()
    while time.monotonic() < deadline_at:
        names_seen = {r["name"] for r in _records(log)}
        if want <= names_seen:
            break
        time.sleep(0.05)
    time.sleep(0.5)
    gateway_path = f"{stubs}:/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return Boot(proc.returncode, proc.stdout, proc.stderr, _records(log), gateway_path)


def _records(log: pathlib.Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@posix_only
def test_a_normal_boot_tail_is_unchanged(tmp_path):
    b = _boot_tail(tmp_path)
    assert b.rc == 0, b.stderr
    assert collections.Counter(b.cmds()) == collections.Counter(
        ["mcp-server", "prepare", "gateway", "watcher", "mcp-list", "runtime"]
    )
    assert b.cmds().index("prepare") < b.cmds().index("gateway")
    assert b.cmds().index("mcp-list") < b.cmds().index("runtime")
    (server,) = [r for r in b.records if r["name"] == "mcp-server"]
    assert server["argv"] == ["-m", "molecule_runtime.a2a_mcp_server", "--transport", "http", "--port", "9100"]
    assert server["env"] == {"HOME": "/tmp", "CONFIGS_DIR": "/configs"}
    assert b.env("prepare") == {"HOME": "/tmp", "CONFIGS_DIR": "/configs", "MOLECULE_RUNTIME_PREPARE_ONLY": "1"}
    assert b.env("gateway") == {"HOME": "/tmp", "HERMES_GATEWAY_BUSY_INPUT_MODE": "queue", "PATH": b.gateway_path}
    watcher = {k: v for k, v in b.env("watcher").items() if k.startswith("MCPWATCH_")}
    assert watcher.pop("MCPWATCH_GATEWAY_PID").isdigit()
    assert watcher == {
        "MCPWATCH_CONFIG": str(tmp_path / "hermes" / "config.yaml"),
        "MCPWATCH_LAUNCH_CMD": (
            f"exec  env HOME=/tmp HERMES_GATEWAY_BUSY_INPUT_MODE=queue PATH={b.gateway_path}"
            " bash -c 'cd /tmp && hermes gateway'"
        ),
        "MCPWATCH_HEALTH_URL": "http://127.0.0.1:8642/health",
        "MCPWATCH_LOG_FILE": str(tmp_path / "hermes" / "gateway.log"),
        "MCPWATCH_MARKER": "/tmp/.hermes/.clean_shutdown",
    }
    assert b.env("runtime") == {"HOME": "/tmp", "CONFIGS_DIR": "/configs"}
    assert "Light run" not in b.stdout + b.stderr


@posix_only
@pytest.mark.parametrize("prepare_rc,prepare", [(1, True), (0, False)])
def test_a_normal_boot_still_falls_back_to_the_watcher(tmp_path, prepare_rc, prepare):
    b = _boot_tail(tmp_path, prepare_rc=prepare_rc, prepare=prepare)
    assert b.rc == 0, b.stderr
    assert {"gateway", "watcher", "runtime"} <= set(b.cmds())


@posix_only
def test_a_run_keeps_mcp_server_prepare_and_gateway_and_starts_no_watcher(tmp_path):
    b = _boot_tail(tmp_path, mode="once", deadline="900")
    assert b.rc == 0, b.stderr
    assert collections.Counter(b.cmds()) == collections.Counter(
        ["mcp-server", "prepare", "gateway", "mcp-list", "runtime"]
    )
    assert b.cmds().index("prepare") < b.cmds().index("gateway")
    # molecule-runtime-prepare sees the run mode, so the runtime's prepare is quiet too.
    assert b.env("prepare")["MOLECULE_RUN_MODE"] == "once"
    assert b.env("gateway")["HERMES_GATEWAY_BUSY_INPUT_MODE"] == "queue"
    runtime = b.env("runtime")
    assert runtime["MOLECULE_RUN_MODE"] == "once"
    assert runtime["HERMES_TURN_REPLY_DEADLINE_SECONDS"] == runtime["HERMES_TURN_MAX_SECONDS"] == "900"
    assert "Light run: no MCP reconcile watcher" in b.stdout


@posix_only
@pytest.mark.parametrize("prepare_rc,prepare", [(1, True), (0, False)])
def test_a_run_without_its_prepared_config_refuses(tmp_path, prepare_rc, prepare):
    b = _boot_tail(tmp_path, mode="once", prepare_rc=prepare_rc, prepare=prepare)
    assert b.rc == 1
    assert not {"gateway", "watcher", "runtime"} & set(b.cmds())
    assert "Light run" in b.stderr and "molecule-runtime-prepare" in b.stderr
