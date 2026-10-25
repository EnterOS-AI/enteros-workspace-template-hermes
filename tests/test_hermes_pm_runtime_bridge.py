"""hermes >= v0.22 package-manager install, bridged into the gateway's home.

The image installs hermes as the agent with HOME=/home/agent, so hermes'
package manager (pm) records the tool store in /home/agent/.hermes/tools and
the selected dependency environment under /home/agent/.hermes/installs. The
gateway runs with HOME=/tmp, its home on the /configs volume, where pm finds
neither: without the bridge it exits with "no dependency environment is
committed for this install", and without HERMES_RUNTIME_DIR it finds no Node
for MCP servers. These tests hold the bridge's pieces in place; the image
build and CI's in-image round trip (validate-runtime) prove it end to end.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
START_SH = REPO / "start.sh"
E2E = REPO / "scripts" / "e2e_full_chain.py"
_BLOCK_RE = re.compile(r"^\{$\n(.*?)^\} >\"\$HERMES_CONFIG\"$", re.M | re.S)
_INSTALLS_BLOCK_RE = re.compile(
    r"^HERMES_BUILD_INSTALLS=/home/agent/\.hermes/installs$\n.*?^fi$", re.M | re.S
)


def _code_lines(path: pathlib.Path) -> list[str]:
    return [
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _dockerfile_instructions() -> list[str]:
    joined = re.sub(r"\\\n", " ", "\n".join(_code_lines(DOCKERFILE)))
    return [ln.strip() for ln in joined.splitlines() if ln.strip()]


def test_image_points_pm_at_the_baked_store_and_forbids_lazy_installs():
    env = " ".join(ln for ln in _dockerfile_instructions() if ln.startswith("ENV "))
    assert "HERMES_RUNTIME_DIR=/home/agent/.hermes/tools" in env, (
        "without HERMES_RUNTIME_DIR the gateway (HOME=/tmp) looks for hermes' "
        "tool store under /tmp/.hermes/tools and finds no node/npx or Chromium"
    )
    assert "HERMES_DISABLE_LAZY_INSTALLS=1" in env, (
        "with lazy installs allowed, a launch or first use can build a new "
        "dependency environment without the A2A plugin, or reach the network at boot"
    )


def test_extras_are_selected_before_the_plugin_is_installed():
    """Enabling extras builds a new environment; the plugin must go in after."""
    runs = [ln for ln in _dockerfile_instructions() if ln.startswith("RUN ")]
    extras = [i for i, ln in enumerate(runs) if "hermes pm install" in ln and "--extra" in ln]
    plugin = [i for i, ln in enumerate(runs) if "hermes-post-install.sh" in ln]
    assert extras and plugin, "the extras step or the plugin post-install step is gone"
    assert max(extras) < min(plugin), (
        "an extras install after the plugin builds an environment without it"
    )


def test_start_sh_links_the_baked_state_before_the_gateway_starts():
    code = _code_lines(START_SH)
    link = next((i for i, ln in enumerate(code)
                 if 'ln -sfn "$HERMES_BUILD_INSTALLS" /configs/.hermes/installs' in ln), None)
    launch = next((i for i, ln in enumerate(code)
                   if "hermes gateway" in ln and "nohup" in code[i - 1] + ln), None)
    assert link is not None, "start.sh no longer links the baked dependency state"
    assert launch is not None, "could not find the gateway launch in start.sh"
    assert link < launch, "the installs link must exist before hermes starts"


def _run_installs_block(tmp_path: pathlib.Path) -> pathlib.Path:
    """Execute start.sh's real installs-link block against a temp root."""
    match = _INSTALLS_BLOCK_RE.search(START_SH.read_text(encoding="utf-8"))
    assert match, "could not locate the installs-link block in start.sh"
    baked = tmp_path / "home-agent-hermes" / "installs"
    (baked / "08f7d59f9758a75d").mkdir(parents=True)
    volume = tmp_path / "configs" / ".hermes"
    volume.mkdir(parents=True, exist_ok=True)
    block = (match.group(0)
             .replace("/home/agent/.hermes/installs", str(baked))
             .replace("/configs/.hermes", str(volume)))
    script = "\n".join(["set -euo pipefail", "chown() { :; }", block])
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return volume / "installs"


def test_installs_link_is_created_on_a_fresh_volume(tmp_path):
    link = _run_installs_block(tmp_path)
    assert link.is_symlink()
    assert (link / "08f7d59f9758a75d").is_dir()


def test_installs_link_replaces_state_a_hermes_process_created(tmp_path):
    stale = tmp_path / "configs" / ".hermes" / "installs" / "stale-key"
    stale.mkdir(parents=True)
    link = _run_installs_block(tmp_path)
    assert link.is_symlink(), "a real directory must be replaced, not nested into"
    assert not (link / "stale-key").exists()
    assert (link / "08f7d59f9758a75d").is_dir()


def test_installs_link_is_repointed_on_every_boot(tmp_path):
    volume = tmp_path / "configs" / ".hermes"
    volume.mkdir(parents=True)
    old = tmp_path / "old-image-installs"
    old.mkdir()
    (volume / "installs").symlink_to(old, target_is_directory=True)
    link = _run_installs_block(tmp_path)
    assert link.resolve() == (tmp_path / "home-agent-hermes" / "installs").resolve()


def test_rendered_config_keeps_ended_session_pruning_on(tmp_path):
    """v0.22 turned sessions.auto_prune off and asks on low disk; nobody answers."""
    match = _BLOCK_RE.search(START_SH.read_text(encoding="utf-8"))
    assert match, "could not locate the config.yaml seed block in start.sh"
    out = tmp_path / "config.yaml"
    block = match.group(0).replace('} >"$HERMES_CONFIG"', f'}} >"{out}"')
    script = "\n".join(["set -euo pipefail", 'DEFAULT_MODEL="m"', 'PROVIDER="custom"', block])
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "MOLECULE_"))}
    run_env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")])
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=run_env, cwd=REPO, check=False)
    assert proc.returncode == 0, proc.stderr
    cfg = yaml.safe_load(out.read_text(encoding="utf-8")) or {}
    assert cfg.get("sessions") == {"auto_prune": True}


def test_e2e_harness_applies_the_same_bridge():
    """The round-trip gate must exercise the image's runtime shape, not bypass it."""
    src = E2E.read_text(encoding="utf-8")
    assert '(hermes_home / "installs").symlink_to(' in src
    assert 'os.environ.setdefault("HERMES_RUNTIME_DIR"' in src
    assert 'os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")' in src
