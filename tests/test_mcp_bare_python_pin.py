"""Bare python MCP commands are pinned to the runtime's interpreter (hermes v0.22).

hermes v0.22 puts its package-manager environment's bin dir first on the
gateway's PATH, outside its active home, so a stdio MCP server whose command
is a bare `python`/`python3` would start hermes' own Python 3.14 instead of
the interpreter the platform's packages live in (adapter._pin_bare_python_command).
"""
from __future__ import annotations

import sys

import pytest
import yaml

from adapter import HermesAgentAdapter
from molecule_runtime.adapters.base import AdapterConfig


@pytest.mark.parametrize("bare", ["python", "python3"])
def test_register_mcp_server_pins_a_bare_python_command(monkeypatch, tmp_path, bare):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    HermesAgentAdapter().register_mcp_server_hook(
        AdapterConfig(model="hermes-test"),
        "plugin-mcp",
        {"command": bare, "args": ["-m", "some_plugin.server"]},
    )
    written = yaml.safe_load((home / "config.yaml").read_text())["mcp_servers"]["plugin-mcp"]
    assert written["command"] == sys.executable
    assert written["args"] == ["-m", "some_plugin.server"]


def test_register_mcp_server_leaves_other_commands_alone(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name, spec in {
        "npx-server": {"command": "npx", "args": ["-y", "@molecule-ai/mcp-server"]},
        "sh-server": {"command": "sh", "args": ["-c", "exec npx -y x"]},
        "abs-python": {"command": "/opt/venv/bin/python3", "args": ["-m", "x"]},
    }.items():
        HermesAgentAdapter().register_mcp_server_hook(AdapterConfig(model="hermes-test"), name, spec)
    servers = yaml.safe_load((home / "config.yaml").read_text())["mcp_servers"]
    assert servers["npx-server"]["command"] == "npx"
    assert servers["sh-server"]["command"] == "sh"
    assert servers["abs-python"]["command"] == "/opt/venv/bin/python3"


def test_register_mcp_server_pinned_command_is_idempotent(monkeypatch, tmp_path):
    """A repeat register of the same bare-python spec must not rewrite the file."""
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = {"command": "python3", "args": ["-m", "x"]}
    adapter = HermesAgentAdapter()
    adapter.register_mcp_server_hook(AdapterConfig(model="hermes-test"), "p", dict(spec))
    first = (home / "config.yaml").stat().st_mtime_ns
    adapter.register_mcp_server_hook(AdapterConfig(model="hermes-test"), "p", dict(spec))
    assert (home / "config.yaml").stat().st_mtime_ns == first
