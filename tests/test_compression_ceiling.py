"""start.sh compaction ceiling (HERMES_COMPRESSION_THRESHOLD_TOKENS).

Executes the real config.yaml seed block out of start.sh (the same extraction
test_memory_skill_knobs.py uses) and parses what it renders. hermes at the
pinned commit reads compression.threshold_tokens in
agent/agent_init.py::_parse_compression_config and compacts at the lower of it
and the window ratio (agent/context_compressor.py::_apply_threshold_tokens_cap).
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
START_SH = REPO / "start.sh"
_BLOCK_RE = re.compile(r"^\{$\n(.*?)^\} >\"\$HERMES_CONFIG\"$", re.M | re.S)
KNOB = "HERMES_COMPRESSION_THRESHOLD_TOKENS"
DEFAULT = 262144


def _render(out: pathlib.Path, **env: str) -> tuple[dict, str]:
    """Run the seed block with ``env`` and return (parsed YAML, stderr)."""
    match = _BLOCK_RE.search(START_SH.read_text(encoding="utf-8"))
    assert match, "could not locate the config.yaml seed block in start.sh"
    block = match.group(0).replace('} >"$HERMES_CONFIG"', f'}} >"{out}"')
    script = "\n".join(
        ["set -euo pipefail", 'DEFAULT_MODEL="m"', 'PROVIDER="custom"', block]
    )
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


def test_unset_writes_the_template_ceiling(tmp_path):
    cfg, stderr = _render(tmp_path / "config.yaml")
    # Only the ceiling: every other compression key keeps hermes' default
    # (load_config merges DEFAULT_CONFIG under this partial section).
    assert cfg["compression"] == {"threshold_tokens": DEFAULT}
    assert type(cfg["compression"]["threshold_tokens"]) is int
    assert "WARNING" not in stderr


def test_a_valid_value_replaces_the_ceiling(tmp_path):
    cfg, stderr = _render(tmp_path / "config.yaml", **{KNOB: "400000"})
    assert cfg["compression"] == {"threshold_tokens": 400000}
    assert "WARNING" not in stderr


def test_the_hermes_minimum_context_is_accepted(tmp_path):
    cfg, stderr = _render(tmp_path / "config.yaml", **{KNOB: "64000"})
    assert cfg["compression"] == {"threshold_tokens": 64000}
    assert "WARNING" not in stderr


def test_off_leaves_hermes_on_the_window_ratio(tmp_path):
    cfg, stderr = _render(tmp_path / "config.yaml", **{KNOB: "off"})
    assert "compression" not in cfg
    assert "WARNING" not in stderr


@pytest.mark.parametrize("raw", ["abc", "-5", "1.5", "0", "010", " 262144", "63999", "1e6", "9999999999", "OFF"])
def test_an_invalid_value_keeps_the_ceiling_and_says_so(tmp_path, raw):
    cfg, stderr = _render(tmp_path / "config.yaml", **{KNOB: raw})
    # A typo must not silently remove the ceiling.
    assert cfg["compression"] == {"threshold_tokens": DEFAULT}
    assert "WARNING" in stderr and KNOB in stderr, stderr
    assert "the template default 262144 applies" in stderr, stderr


def test_other_knob_warnings_keep_their_wording(tmp_path):
    _cfg, stderr = _render(tmp_path / "config.yaml", HERMES_MEMORY_CHAR_LIMIT="abc")
    assert "hermes keeps its built-in default" in stderr, stderr
