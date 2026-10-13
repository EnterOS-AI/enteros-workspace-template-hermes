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


def test_registers_only_the_post_tool_call_observer():
    """pre_tool_call is a policy hook: a callback that raises or times out
    there blocks the tool. A liveness ping must never be able to do that."""
    ctx = _Ctx()
    _plugin().register(ctx)
    assert [name for name, _ in ctx.hooks] == ["post_tool_call"]


def test_manifest_matches_what_start_sh_enables():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_DIR.name == "molecule_turn_activity"
    # Explicit, so hermes never auto-detects it as a memory/model provider.
    assert manifest["kind"] == "standalone"
    assert manifest["provides_hooks"] == ["post_tool_call"]


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


def _run_block(tmp_path: pathlib.Path, *, cwd: pathlib.Path = REPO, **env: str):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    log = tmp_path / "install.log"
    script = "\n".join([
        "set -euo pipefail",
        _INSTALL_STUB,
        f'HERMES_HOME="{hermes_home}"',
        _liveness_block(),
        'printf "EXPORTED=%s\\n" "$(printenv MOLECULE_TOOL_ACTIVITY_FILE)"',
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
    gateway = next(i for i, ln in code if "&& hermes gateway" in ln)
    runtime = next(i for i, ln in code if re.search(r"\bmolecule-runtime$", ln.strip()) and "exec" in ln
                   and "MOLECULE_SMOKE_MODE" not in ln and i > gateway)
    assert export < gateway < runtime


def test_the_image_ships_the_plugin_source():
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY hermes_plugins/ /app/hermes_plugins/$", text, re.M)
