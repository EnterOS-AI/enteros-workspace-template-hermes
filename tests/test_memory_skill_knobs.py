"""start.sh memory / skill-review knobs (issue #383).

Executes the real config.yaml seed block out of start.sh (the same extraction
test_toolsearch_concierge_scope.py uses) under start.sh's own shell options and
parses what it renders. hermes at the pinned commit reads these keys at agent
construction: memory.memory_char_limit / user_char_limit / nudge_interval in
agent/agent_init.py::_init_memory and skills.creation_nudge_interval in
agent/agent_init.py::_apply_agent_section.
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

_KNOBS = {
    "HERMES_MEMORY_CHAR_LIMIT": ("memory", "memory_char_limit", 1),
    "HERMES_USER_CHAR_LIMIT": ("memory", "user_char_limit", 1),
    "HERMES_MEMORY_NUDGE_INTERVAL": ("memory", "nudge_interval", 0),
    "HERMES_SKILL_NUDGE_INTERVAL": ("skills", "creation_nudge_interval", 0),
}


def _render(out: pathlib.Path, **env: str) -> tuple[dict, str]:
    """Run the seed block with ``env`` and return (parsed YAML, stderr)."""
    match = _BLOCK_RE.search(START_SH.read_text(encoding="utf-8"))
    assert match, "could not locate the config.yaml seed block in start.sh"
    block = match.group(0).replace('} >"$HERMES_CONFIG"', f'}} >"{out}"')
    script = "\n".join(
        ["set -euo pipefail", 'DEFAULT_MODEL="m"', 'PROVIDER="custom"', block]
    )
    # The block shells out to python3 + PyYAML for the MCP seed, so inherit the
    # environment (a setup-python interpreter needs its LD_LIBRARY_PATH) minus
    # anything that steers start.sh, and put this interpreter first on PATH.
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


def test_unset_knobs_leave_hermes_defaults(tmp_path):
    cfg, stderr = _render(tmp_path / "config.yaml")
    assert "memory" not in cfg
    assert "skills" not in cfg
    assert "WARNING" not in stderr


def test_every_knob_renders_as_a_bare_integer(tmp_path):
    cfg, stderr = _render(
        tmp_path / "config.yaml",
        HERMES_MEMORY_CHAR_LIMIT="6000",
        HERMES_USER_CHAR_LIMIT="3000",
        HERMES_MEMORY_NUDGE_INTERVAL="0",
        HERMES_SKILL_NUDGE_INTERVAL="0",
    )
    assert cfg["memory"] == {
        "memory_char_limit": 6000,
        "user_char_limit": 3000,
        "nudge_interval": 0,
    }
    assert cfg["skills"] == {"creation_nudge_interval": 0}
    # MemoryStore receives these uncoerced; a str would break every limit check.
    assert all(type(v) is int for v in (*cfg["memory"].values(), *cfg["skills"].values()))
    assert "WARNING" not in stderr


@pytest.mark.parametrize("name", sorted(_KNOBS))
def test_one_knob_writes_only_its_own_key(tmp_path, name):
    section, key, _minimum = _KNOBS[name]
    cfg, _stderr = _render(tmp_path / "config.yaml", **{name: "25"})
    assert cfg[section] == {key: 25}
    other = {"memory": "skills", "skills": "memory"}[section]
    assert other not in cfg


@pytest.mark.parametrize("name", sorted(_KNOBS))
@pytest.mark.parametrize("raw", ["abc", "-5", "1.5", "010", " 7", "7 ", "1e3", "9999999999"])
def test_invalid_values_are_refused_loudly(tmp_path, name, raw):
    section, key, _minimum = _KNOBS[name]
    cfg, stderr = _render(tmp_path / "config.yaml", **{name: raw})
    assert key not in (cfg.get(section) or {}), f"{name}={raw!r} reached config.yaml"
    assert "WARNING" in stderr and name in stderr, stderr


@pytest.mark.parametrize(
    "name, accepted",
    [
        ("HERMES_MEMORY_CHAR_LIMIT", False),
        ("HERMES_USER_CHAR_LIMIT", False),
        ("HERMES_MEMORY_NUDGE_INTERVAL", True),
        ("HERMES_SKILL_NUDGE_INTERVAL", True),
    ],
)
def test_zero_disables_a_review_but_is_not_a_char_limit(tmp_path, name, accepted):
    section, key, _minimum = _KNOBS[name]
    cfg, stderr = _render(tmp_path / "config.yaml", **{name: "0"})
    if accepted:
        assert cfg[section] == {key: 0}
        assert "WARNING" not in stderr
    else:
        assert key not in (cfg.get(section) or {})
        assert name in stderr


def test_runtime_skills_merge_keeps_the_nudge_interval(tmp_path, monkeypatch):
    """The runtime adds skills.external_dirs to the same mapping after boot.

    Neither writer may clobber the other: the nudge interval must survive the
    runtime's merge, and external_dirs must still be added.
    """
    from molecule_runtime.skills_render import materialize_hermes_skills

    home = tmp_path / ".hermes"
    home.mkdir()
    _render(home / "config.yaml", HERMES_SKILL_NUDGE_INTERVAL="0")
    monkeypatch.setenv("HERMES_HOME", str(home))
    configs = tmp_path / "configs"

    materialize_hermes_skills(configs)

    skills = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["skills"]
    assert skills["creation_nudge_interval"] == 0
    assert skills["external_dirs"] == [str(configs / "skills")]
