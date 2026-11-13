"""Turn-lease liveness feed from the hermes gateway (runtime#417).

The runtime judges whether a turn is still working from the mtime of
MOLECULE_TOOL_ACTIVITY_FILE. The gateway runs the tools but is started once by
start.sh, so the runtime's per-turn export never reached it and the lease read
idle for the container's whole life. start.sh now fixes one path at boot for
both processes, and the molecule_turn_activity hermes plugin touches it after
every tool call. scripts/e2e_full_chain.py proves the same chain against the
real pinned gateway in CI (gateway-live-roundtrip).
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "hermes_plugins" / "molecule_turn_activity"
START_SH = REPO / "start.sh"
ENV = "MOLECULE_TOOL_ACTIVITY_FILE"
MODEL_CALL_ENV = "MOLECULE_MODEL_CALL_FILE"

# What hermes passes the two model-call observers (agent/api_request_hooks.py and
# agent/turn_response_intake.py at HERMES_COMMIT), for the Phase 1 gate's call:
# the platform LLM answered 402 insufficient_balance.
REFUSED_CALL = dict(
    task_id="t1", turn_id="turn-1", api_request_id="turn-1:api:1", session_id="s1", platform="molecule-a2a",
    model="minimax/MiniMax-M2.7", provider="custom", base_url="http://llm-proxy.internal/v1",
    api_mode="chat_completions", api_call_count=1, api_duration=0.4, started_at=1.0, ended_at=1.4,
    status_code=402, retry_count=0, max_retries=3, retryable=False, reason="billing",
    error={"type": "APIStatusError", "message": "Error code: 402 - {'error': {'code': 'insufficient_balance'}}"},
    request={"method": "POST", "body": {"messages": [{"role": "user", "content": "write the weekly report"}]}},
)
ANSWERED_CALL = dict(
    task_id="t1", turn_id="turn-1", api_request_id="turn-1:api:2", session_id="s1", platform="molecule-a2a",
    model="minimax/MiniMax-M2.7", provider="custom", api_call_count=2, finish_reason="stop",
    response={"assistant_message": {"content": "the weekly report"}}, usage={"total_tokens": 9},
)


def _plugin():
    spec = importlib.util.spec_from_file_location(
        "molecule_turn_activity_under_test", PLUGIN_DIR / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ctx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


# --- the plugin ------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_call_file,want",
    [
        (None, ["post_tool_call"]),  # a workspace: exactly as before
        ("/tmp/molecule-turn-lease/model_call", ["post_tool_call", "post_api_request", "api_request_error"]),
    ],
)
def test_registers_observers_only(monkeypatch, model_call_file, want):
    """pre_tool_call is a policy hook: a callback that raises or times out
    there blocks the tool. A liveness ping must never be able to do that. The
    two model-call observers exist only in a Light run, where start.sh sets
    MOLECULE_MODEL_CALL_FILE; a workspace's gateway registers nothing new, so
    hermes builds no extra hook payloads there."""
    if model_call_file is None:
        monkeypatch.delenv(MODEL_CALL_ENV, raising=False)
    else:
        monkeypatch.setenv(MODEL_CALL_ENV, model_call_file)
    ctx = _Ctx()
    _plugin().register(ctx)
    assert [name for name, _ in ctx.hooks] == want


def test_manifest_matches_what_start_sh_enables():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_DIR.name == "molecule_turn_activity"
    # Explicit, so hermes never auto-detects it as a memory/model provider.
    assert manifest["kind"] == "standalone"
    assert manifest["provides_hooks"] == ["post_tool_call", "post_api_request", "api_request_error"]


def test_touch_advances_the_mtime(tmp_path, monkeypatch):
    activity = tmp_path / "tool_activity"
    activity.write_text("")
    stale = time.time() - 60
    os.utime(activity, (stale, stale))
    monkeypatch.setenv(ENV, str(activity))
    # hermes passes the full observer payload as keyword arguments.
    _plugin().touch_activity_file(tool_name="terminal", args={}, result="ok", tool_call_id="c1")
    assert activity.stat().st_mtime > stale + 30


def test_touch_creates_a_missing_file_private(tmp_path, monkeypatch):
    activity = tmp_path / "tool_activity"
    monkeypatch.setenv(ENV, str(activity))
    _plugin().touch_activity_file()
    assert stat.S_IMODE(activity.stat().st_mode) & 0o077 == 0


def test_touch_without_the_variable_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert _plugin().touch_activity_file(tool_name="terminal") is None
    assert list(tmp_path.iterdir()) == []


def test_touch_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-dir" / "tool_activity"))
    assert _plugin().touch_activity_file() is None


def test_each_tool_call_rewrites_the_record_with_the_next_seq(tmp_path, monkeypatch):
    activity = tmp_path / "tool_activity"
    monkeypatch.setenv(ENV, str(activity))
    plugin = _plugin()
    plugin.touch_activity_file(
        tool_name="terminal", args={"command": "ls"}, status="ok", duration_ms=12
    )
    first = json.loads(activity.read_text(encoding="utf-8"))
    plugin.touch_activity_file(tool_name="read_file", args={"path": "/x"}, status="error")
    second = json.loads(activity.read_text(encoding="utf-8"))

    assert first["v"] == 1
    assert (first["tool"], first["status"], first["duration_ms"]) == ("terminal", "ok", 12)
    assert json.loads(first["args"]) == {"command": "ls"}
    assert second["seq"] == first["seq"] + 1
    assert second["boot"] == first["boot"]
    assert (second["tool"], second["status"]) == ("read_file", "error")
    # Atomic replace: no temp file is left behind, and the file stays private.
    assert [p.name for p in tmp_path.iterdir()] == ["tool_activity"]
    assert stat.S_IMODE(activity.stat().st_mode) & 0o077 == 0


def test_a_new_gateway_process_gets_a_new_boot(tmp_path, monkeypatch):
    activity = tmp_path / "tool_activity"
    monkeypatch.setenv(ENV, str(activity))
    _plugin().touch_activity_file(tool_name="terminal")
    before = json.loads(activity.read_text(encoding="utf-8"))
    _plugin().touch_activity_file(tool_name="terminal")
    after = json.loads(activity.read_text(encoding="utf-8"))
    assert before["boot"] != after["boot"]
    assert after["seq"] == 1


def test_args_are_bounded_and_odd_ones_never_raise(tmp_path, monkeypatch):
    activity = tmp_path / "tool_activity"
    monkeypatch.setenv(ENV, str(activity))
    plugin = _plugin()
    plugin.touch_activity_file(tool_name="terminal", args={"command": "x" * 10_000})
    assert len(json.loads(activity.read_text(encoding="utf-8"))["args"]) <= 400
    plugin.touch_activity_file(tool_name="terminal", args="raw string")
    assert json.loads(activity.read_text(encoding="utf-8"))["args"] == "raw string"
    assert plugin.touch_activity_file(tool_name="terminal", args={"obj": object()}) is None
    assert plugin.touch_activity_file(tool_name=None, args=None, duration_ms="slow") is None
    record = json.loads(activity.read_text(encoding="utf-8"))
    assert (record["tool"], record["duration_ms"]) == ("", 0)


def test_the_executor_reads_what_the_plugin_writes(tmp_path, monkeypatch):
    """The record is a contract between two processes; pin both ends."""
    import executor

    activity = tmp_path / "tool_activity"
    monkeypatch.setenv(ENV, str(activity))
    _plugin().touch_activity_file(tool_name="terminal", args={"command": "pwd"}, status="ok")
    record = executor._read_tool_event(str(activity))
    assert record is not None and record["tool"] == "terminal"
    assert executor._tool_chip_summary(record["tool"], record["args"]) == "🛠 terminal: pwd"


# --- model calls (Light runs) ------------------------------------------------------


def test_a_refused_model_call_is_recorded_as_hermes_classified_it(tmp_path, monkeypatch):
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv(MODEL_CALL_ENV, str(model_calls))
    before = time.time()
    assert _plugin().record_model_call_error(**REFUSED_CALL) is None
    record = json.loads(model_calls.read_text(encoding="utf-8"))
    assert record.pop("ts") >= before
    assert record == {
        "v": 1, "ok": False, "status_code": 402, "reason": "billing", "retryable": False,
        "provider": "custom", "model": "minimax/MiniMax-M2.7",
    }
    # The verdict only: neither the provider's error text nor the request (the
    # prompt) leaves the gateway through this file. Private, no temp file left.
    text = model_calls.read_text(encoding="utf-8")
    assert "insufficient_balance" not in text and "weekly report" not in text
    assert [p.name for p in tmp_path.iterdir()] == ["model_call"]
    assert stat.S_IMODE(model_calls.stat().st_mode) & 0o077 == 0


def test_a_model_call_that_answered_replaces_the_refusal(tmp_path, monkeypatch):
    # A retry or a fallback provider that answers after a refusal: the turn
    # recovered, so the last record must say so.
    model_calls = tmp_path / "model_call"
    monkeypatch.setenv(MODEL_CALL_ENV, str(model_calls))
    plugin = _plugin()
    plugin.record_model_call_error(**REFUSED_CALL)
    assert plugin.record_model_call_ok(**ANSWERED_CALL) is None
    record = json.loads(model_calls.read_text(encoding="utf-8"))
    assert (record["ok"], record["status_code"], record["reason"]) == (True, None, "")


def test_model_call_records_need_the_variable_and_never_raise(tmp_path, monkeypatch):
    monkeypatch.delenv(MODEL_CALL_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    plugin = _plugin()
    assert plugin.record_model_call_error(**REFUSED_CALL) is None
    assert plugin.record_model_call_ok(**ANSWERED_CALL) is None
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setenv(MODEL_CALL_ENV, str(tmp_path / "no-such-dir" / "model_call"))
    assert plugin.record_model_call_error(**REFUSED_CALL) is None
    assert plugin.record_model_call_error(status_code="402", retryable="no", reason=None) is None


def test_the_executor_reads_what_the_plugin_records_for_a_model_call(tmp_path, monkeypatch):
    """The model-call record is a contract between two processes; pin both ends."""
    import executor

    monkeypatch.setenv(MODEL_CALL_ENV, str(tmp_path / "model_call"))
    turn_started = time.time()
    _plugin().record_model_call_error(**REFUSED_CALL)
    assert executor._provider_refusal(turn_started) == (
        "the model provider refused the call (billing, HTTP 402; custom minimax/MiniMax-M2.7)"
    )
    _plugin().record_model_call_ok(**ANSWERED_CALL)
    assert executor._provider_refusal(turn_started) is None


# --- start.sh: enabling it ----------------------------------------------------------

_SEED_RE = re.compile(r"^\{$\n(.*?)^\} >\"\$HERMES_CONFIG\"$", re.M | re.S)


def _render_seed(out: pathlib.Path, **env: str) -> dict:
    block = _SEED_RE.search(START_SH.read_text(encoding="utf-8")).group(0)
    block = block.replace('} >"$HERMES_CONFIG"', f'}} >"{out}"')
    # Inherit the environment (the block runs python3, and a setup-python
    # interpreter needs its LD_LIBRARY_PATH) minus anything that steers start.sh.
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "MOLECULE_"))}
    run_env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")])
    proc = subprocess.run(
        ["bash", "-c", "\n".join(["set -euo pipefail", 'DEFAULT_MODEL="m"', 'PROVIDER="custom"', block])],
        capture_output=True, text=True, cwd=REPO, env={**run_env, **env}, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return yaml.safe_load(out.read_text(encoding="utf-8"))


def test_enabled_alongside_the_a2a_platform(tmp_path):
    cfg = _render_seed(tmp_path / "config.yaml")
    assert cfg["plugins"]["enabled"] == ["molecule_turn_activity", "molecule_a2a"]
    assert "molecule-a2a" in cfg["platforms"]


def test_enabled_on_the_chat_completions_transport_too(tmp_path):
    cfg = _render_seed(tmp_path / "config.yaml", MOLECULE_A2A_PLATFORM_ENABLED="false")
    assert cfg["plugins"]["enabled"] == ["molecule_turn_activity"]
    assert "platforms" not in cfg


# --- start.sh: the boot block ----------------------------------------------------------


def _liveness_block() -> str:
    lines = START_SH.read_text(encoding="utf-8").splitlines()
    first = next(i for i, ln in enumerate(lines) if "--- Turn-lease liveness feed" in ln)
    last = next(i for i, ln in enumerate(lines) if "--- Start the Molecule A2A MCP HTTP server" in ln)
    return "\n".join(lines[first:last])


# Records every `install` call; really runs it (minus -o/-g, there is no
# `agent` user here) only for paths inside the test's temp dir, so the block's
# default /tmp/molecule-turn-lease is asserted without being created.
_INSTALL_STUB = r'''
install() {
  printf '%s\n' "install $*" >> "$INSTALL_LOG"
  local args=() skip=""
  for a in "$@"; do
    if [ -n "$skip" ]; then skip=""; continue; fi
    case "$a" in -o|-g) skip=1 ;; *) args+=("$a") ;; esac
  done
  case "${args[${#args[@]}-1]}" in "$SANDBOX"/*) command install "${args[@]}" ;; esac
}
'''


def _run_block(tmp_path: pathlib.Path, *, cwd: pathlib.Path = REPO, run_once: str | None = None, **env: str):
    """Run the liveness block; ``run_once`` is the RUN_ONCE the run-mode block
    above it left (None: not set at all, as in a start.sh without that block)."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    log = tmp_path / "install.log"
    script = "\n".join([
        "set -euo pipefail",
        _INSTALL_STUB,
        f'HERMES_HOME="{hermes_home}"',
        *([f"RUN_ONCE={run_once}"] if run_once is not None else []),
        _liveness_block(),
        'printf "EXPORTED=%s\\n" "$(printenv MOLECULE_TOOL_ACTIVITY_FILE)"',
        'printf "MODEL_CALL=%s\\n" "$(printenv MOLECULE_MODEL_CALL_FILE || echo "<unset>")"',
    ])
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=cwd, check=False,
        env={"PATH": os.environ.get("PATH", ""), "INSTALL_LOG": str(log),
             "SANDBOX": str(tmp_path), **env},
    )
    assert proc.returncode == 0, proc.stderr
    exported = re.search(r"^EXPORTED=(.*)$", proc.stdout, re.M).group(1)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return exported, calls, hermes_home, proc


@pytest.mark.parametrize(
    "run_once,preset,want",
    [
        (None, None, "<unset>"),
        ("0", None, "<unset>"),  # a workspace: nothing new in its environment
        ("1", None, "/tmp/molecule-turn-lease/model_call"),
        # Beside the activity file, in the private directory both processes share.
        ("1", "/var/run/molecule/activity", "/var/run/molecule/model_call"),
    ],
)
def test_a_light_run_records_model_calls_beside_the_activity_file(tmp_path, run_once, preset, want):
    env = {} if preset is None else {ENV: preset}
    _exported, _calls, _home, proc = _run_block(tmp_path, run_once=run_once, **env)
    assert re.search(r"^MODEL_CALL=(.*)$", proc.stdout, re.M).group(1) == want


def test_default_path_is_exported_in_its_own_private_dir(tmp_path):
    exported, calls, _home, _proc = _run_block(tmp_path)
    assert exported == "/tmp/molecule-turn-lease/tool_activity"
    # The runtime chmods the file's parent to 0700, so it must be a dedicated dir.
    assert "install -d -m 700 -o agent -g agent /tmp/molecule-turn-lease" in calls


def test_a_preset_path_wins_and_its_directory_is_left_alone(tmp_path):
    preset = "/var/run/molecule/activity"
    exported, calls, _home, _proc = _run_block(tmp_path, MOLECULE_TOOL_ACTIVITY_FILE=preset)
    assert exported == preset
    assert not [c for c in calls if "/var/run/molecule" in c], calls


def test_plugin_is_installed_agent_owned_into_hermes_home(tmp_path):
    _exported, calls, home, _proc = _run_block(tmp_path)
    installed = home / "plugins" / "molecule_turn_activity"
    for name in ("plugin.yaml", "__init__.py"):
        assert (installed / name).read_bytes() == (PLUGIN_DIR / name).read_bytes()
    assert any(c.startswith("install -m 644 -o agent -g agent ") for c in calls), calls


def test_missing_plugin_source_is_loud_but_not_fatal(tmp_path):
    _exported, _calls, home, proc = _run_block(tmp_path, cwd=tmp_path)
    assert "ERROR turn-lease feed plugin missing" in proc.stderr
    assert not (home / "plugins" / "molecule_turn_activity").exists()


def test_exported_before_the_gateway_and_the_runtime_start():
    """Both processes must inherit the same value from start.sh's environment."""
    code = [
        (i, ln) for i, ln in enumerate(START_SH.read_text(encoding="utf-8").splitlines())
        if not ln.lstrip().startswith("#")
    ]
    export = next(i for i, ln in code if ln.strip() == "export MOLECULE_TOOL_ACTIVITY_FILE")
    # The plugin decides at registration, when the gateway loads it, whether to
    # record model calls: the variable must be in the gateway's environment.
    model_call = next(i for i, ln in code if ln.strip() == "export MOLECULE_MODEL_CALL_FILE")
    gateway = next(i for i, ln in code if "&& hermes gateway" in ln)
    runtime = next(i for i, ln in code if re.search(r"\bmolecule-runtime$", ln.strip()) and "exec" in ln
                   and "MOLECULE_SMOKE_MODE" not in ln and i > gateway)
    assert export < gateway < runtime
    assert model_call < gateway


def test_the_image_ships_the_plugin_source():
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY hermes_plugins/ /app/hermes_plugins/$", text, re.M)
