"""start.sh must disable hermes tool-search for the CONCIERGE ONLY.

Why this test exists
--------------------
hermes' tiered disclosure (``tools/tool_search.py``) strips every MCP/plugin
tool out of the model-facing tools array and replaces them with the
``tool_search`` / ``tool_describe`` / ``tool_call`` bridge. ``agent_init.py``
then derives ``agent.valid_tool_names`` from that *stripped* array — while the
bridge description keeps advertising the deferred tools' REAL names. The model
reads a real name off that listing, calls it directly, the plain
set-membership check in ``conversation_loop.py`` misses, and the turn dies with
``Model generated invalid tool call: mcp__molecule_platform__list_orgs``.

For the concierge (which carries the ~110-tool management MCP surface) that is
fatal: the strike budget is spent on orientation calls and
``provision_workspace`` is never reached.

``should_activate()`` fires on the mere EXISTENCE of a deferrable tool —
``threshold_pct`` / ``listing_max_tokens`` bound only how much of the catalog
gets LISTED, not whether it defers. So no budget knob can fix this; only
``tools.tool_search.enabled: "off"`` can.

This test asserts BOTH halves of the contract by executing the real
config.yaml seed block out of ``start.sh`` and parsing what it renders:

  1. a concierge (``/configs/prompts/concierge.md`` delivered) gets
     ``tools.tool_search.enabled == "off"``;
  2. an ordinary workspace does NOT get a ``tools`` key at all — it keeps the
     upstream default and is untouched by this change.

It executes the block extracted verbatim from ``start.sh`` rather than
re-stating the YAML, so a future edit that drops or un-scopes the stanza fails
here instead of silently shipping.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
START_SH = REPO / "start.sh"

# The config.yaml seed is the brace group that redirects into $HERMES_CONFIG.
# Anchored on both delimiters so a moved/renamed block fails loudly rather
# than silently matching nothing.
_BLOCK_RE = re.compile(r"^\{$\n(.*?)^\} >\"\$HERMES_CONFIG\"$", re.M | re.S)


def _seed_block() -> str:
    src = START_SH.read_text(encoding="utf-8")
    m = _BLOCK_RE.search(src)
    assert m, "could not locate the `{ ... } >\"$HERMES_CONFIG\"` seed block in start.sh"
    return m.group(0)


def _render(tmp_path: pathlib.Path, *, is_concierge: bool) -> dict:
    """Run start.sh's real seed block and return the parsed YAML it emits."""
    out = tmp_path / "config.yaml"
    block = _seed_block().replace('} >"$HERMES_CONFIG"', f'}} >"{out}"')
    script = "\n".join(
        [
            "set -uo pipefail",
            # Stubs for the variables the block interpolates. Only IS_CONCIERGE
            # is under test; the rest just need to be defined.
            f"IS_CONCIERGE={1 if is_concierge else 0}",
            'DEFAULT_MODEL="minimax/MiniMax-M2.7"',
            'PROVIDER="custom"',
            'MOLECULE_MCP_PORT="9100"',
            block,
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=REPO
    )
    assert proc.returncode == 0, f"seed block failed: {proc.stderr}"
    return yaml.safe_load(out.read_text(encoding="utf-8")) or {}


def _tool_search(cfg: dict):
    tools = cfg.get("tools")
    if not isinstance(tools, dict):
        return None
    return tools.get("tool_search")


def test_start_sh_is_valid_bash():
    proc = subprocess.run(
        ["bash", "-n", str(START_SH)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_concierge_gets_tool_search_off(tmp_path):
    """The concierge must see its full MCP surface eagerly."""
    ts = _tool_search(_render(tmp_path, is_concierge=True))
    assert isinstance(ts, dict), "concierge config.yaml is missing tools.tool_search"
    # hermes' ToolSearchConfig.from_raw accepts "off"/"false"/"0"/"no";
    # anything else silently falls back to "auto" (= still defers).
    assert str(ts.get("enabled")).strip().lower() in ("off", "false", "0", "no"), (
        f"tools.tool_search.enabled={ts.get('enabled')!r} does not disable deferral; "
        "hermes falls back to 'auto' for unrecognised values"
    )


def test_ordinary_workspace_is_untouched(tmp_path):
    """Scope guard: non-concierge workspaces keep the upstream default."""
    cfg = _render(tmp_path, is_concierge=False)
    assert _tool_search(cfg) is None, (
        "ordinary workspaces must not be given a tools.tool_search override — "
        f"got {cfg.get('tools')!r}"
    )


def test_concierge_flag_is_derived_from_the_persona_graft():
    """IS_CONCIERGE must key on the concierge persona, not a hand-set default."""
    src = START_SH.read_text(encoding="utf-8")
    assert "IS_CONCIERGE=0" in src, "IS_CONCIERGE must default to 0 (fail-closed)"
    assert re.search(
        r'\[ "\$persona" = "/configs/prompts/concierge\.md" \].*IS_CONCIERGE=1', src
    ), "IS_CONCIERGE must be set from the /configs/prompts/concierge.md persona graft"


@pytest.mark.parametrize("is_concierge", [True, False])
def test_seed_block_still_renders_parsable_yaml(tmp_path, is_concierge):
    """The stanza must not corrupt the rest of the seeded config."""
    out_dir = tmp_path / str(is_concierge)
    out_dir.mkdir()
    cfg = _render(out_dir, is_concierge=is_concierge)
    assert cfg.get("model", {}).get("default") == "minimax/MiniMax-M2.7"
    assert "mcp_servers" in cfg, "the molecule MCP seed must survive"
