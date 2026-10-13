"""Bundled Node.js reaches the hermes gateway and the agent's terminal.

The hermes installer puts node/npm/npx in /home/agent/.hermes/node/bin, which
is on neither the gateway PATH nor hermes' runtime HERMES_HOME. hermes' terminal
tool runs commands from a ``bash -l`` snapshot, and Debian's /etc/profile
overwrites PATH there before it sources /etc/profile.d/*.sh. So
scripts/hermes-node-path.sh is both start.sh's gateway-PATH helper and the
image's /etc/profile.d drop-in; these tests execute it rather than re-state it.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "hermes-node-path.sh"
START_SH = REPO / "start.sh"
DOCKERFILE = REPO / "Dockerfile"
# What Debian's /etc/profile assigns for a non-root login shell.
DEBIAN_LOGIN_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games"


def _node_bin(tmp_path: pathlib.Path, *exes: str) -> pathlib.Path:
    bin_dir = tmp_path / "node" / "bin"
    bin_dir.mkdir(parents=True)
    for exe in exes:
        target = bin_dir / exe
        target.write_text("#!/bin/sh\necho bundled\n", encoding="utf-8")
        target.chmod(0o755)
    return bin_dir


def _source(node_bin: pathlib.Path, body: str, path: str = DEBIAN_LOGIN_PATH) -> str:
    """Source the drop-in the way /etc/profile does, then run ``body``."""
    proc = subprocess.run(
        ["sh", "-c", f'. "{SCRIPT}"\n{body}'],
        capture_output=True,
        text=True,
        env={"PATH": path, "HERMES_BUNDLED_NODE_BIN": str(node_bin)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_login_shell_path_gains_node_and_npx(tmp_path):
    """The prod symptom: `which node` was empty in the terminal tool."""
    node_bin = _node_bin(tmp_path, "node", "npx", "npm")
    out = _source(node_bin, "command -v node; command -v npx; printf '%s' \"$PATH\"")
    node, npx, path = out.splitlines()
    assert node == str(node_bin / "node")
    assert npx == str(node_bin / "npx")
    assert path == f"{node_bin}:{DEBIAN_LOGIN_PATH}"


def test_sourcing_twice_does_not_duplicate_the_entry(tmp_path):
    node_bin = _node_bin(tmp_path, "node", "npx")
    path = _source(node_bin, f'. "{SCRIPT}"; printf "%s" "$PATH"')
    assert path.split(":").count(str(node_bin)) == 1


@pytest.mark.parametrize("present", [(), ("node",), ("npx",)])
def test_incomplete_install_leaves_path_alone(tmp_path, present):
    node_bin = _node_bin(tmp_path, *present)
    assert _source(node_bin, 'printf "%s" "$PATH"') == DEBIAN_LOGIN_PATH


def test_non_executable_binaries_leave_path_alone(tmp_path):
    node_bin = _node_bin(tmp_path, "node", "npx")
    (node_bin / "npx").chmod(0o644)
    assert _source(node_bin, 'printf "%s" "$PATH"') == DEBIAN_LOGIN_PATH


def test_the_image_installs_the_drop_in_under_profile_d():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(
        r"^COPY scripts/hermes-node-path\.sh /etc/profile\.d/hermes-node-path\.sh$",
        text,
        re.M,
    ), "the terminal tool's login shells would never see the bundled node"


def _gateway_path_block() -> str:
    lines = START_SH.read_text(encoding="utf-8").splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith('GATEWAY_PATH="'))
    last = next(i for i, ln in enumerate(lines) if 'gateway PATH=' in ln and i > first)
    return "\n".join(lines[first : last + 1])


@pytest.mark.parametrize("installed", [True, False])
def test_start_sh_prepends_the_bundled_bin_to_the_gateway_path(tmp_path, installed):
    node_bin = _node_bin(tmp_path, *(("node", "npx") if installed else ()))
    proc = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _gateway_path_block() + '\nprintf "%s" "$GATEWAY_PATH"'],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"PATH": os.environ.get("PATH", ""), "HERMES_BUNDLED_NODE_BIN": str(node_bin)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    base = "/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    gateway_path = proc.stdout.rsplit("\n", 1)[-1]
    assert gateway_path == (f"{node_bin}:{base}" if installed else base)


def test_every_hermes_launch_uses_the_gateway_path():
    """Boot, the reconcile relaunch, and the CLI smoke must agree on PATH."""
    code = [
        ln for ln in START_SH.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    launches = [ln for ln in code if "HOME=/tmp" in ln and "PATH=" in ln]
    assert len(launches) == 3, launches
    for line in launches:
        assert re.search(r'PATH="?\$\{?GATEWAY_PATH\}?"?', line), line
    literal = [ln for ln in code if "/home/agent/.local/bin:/usr/local/sbin" in ln]
    assert literal == [ln for ln in code if ln.startswith('GATEWAY_PATH="')], literal
