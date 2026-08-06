"""start.sh must disable hermes tool-search for EVERY workspace.

Why this test exists
--------------------
hermes' tiered disclosure (``tools/tool_search.py``) strips every MCP/plugin
tool out of the model-facing tools array and replaces them with the
``tool_search`` / ``tool_describe`` / ``tool_call`` bridge. ``agent_init.py``
then derives ``agent.valid_tool_names`` from that *stripped* array (0.19.0:
``agent_init.py`` ``valid_tool_names = {t["function"]["name"] for t in
agent.tools}``) — while the bridge description keeps advertising the deferred
tools' REAL names. The model reads a real name off that listing, calls it
directly, the plain set-membership check in ``conversation_loop.py`` misses,
and after three strikes the turn dies with
``Model generated invalid tool call: <a real, registered tool id>``.

WHY THIS IS NO LONGER SCOPED TO THE CONCIERGE (2026-08-06)
----------------------------------------------------------
The first cut of this fix emitted the stanza only when
``/configs/prompts/concierge.md`` had been delivered (``IS_CONCIERGE``). That
made a FATAL runtime capability — whether the agent can call any MCP tool at
all — depend on an asset the control plane does not guarantee. molecule-core
``87de7be0c`` documents a hermes concierge that reached production with
``/configs/prompts/`` absent while ``/configs/config.yaml`` still declared
``prompt_files: [prompts/concierge.md]``; on such a box ``IS_CONCIERGE`` is 0,
the stanza is never written, and the concierge's whole 92-tool surface is
deferred behind the bridge. Measured live on two containers running the SAME
image, differing only in that file:

  * persona delivered  -> ``tools:`` present, no ``tool_search activated`` line,
    ``mcp__molecule__send_message_to_user`` dispatches;
  * persona absent     -> no ``tools:`` key,
    ``tool_search activated (tier 1): 18 core/visible tools kept, 92 deferred``.

That state is what makes ``staging-tenant-cd / e2e-smoke`` nondeterministic:
the concierge answers a real ``provision_workspace`` A2A turn with
``Model generated invalid tool call: mcp__molecule__get_workspace_info`` — a
REAL id from the ``molecule`` sidecar's 32 registered tools — and burns the
strike budget before reaching ``provision_workspace``.

Ordinary workspaces are exposed to the identical hazard: a live worker logs
``tool_search activated (tier 1): 18 core/visible tools kept, 38 deferred``.
``should_activate()`` fires on the mere EXISTENCE of a deferrable tool —
``threshold_pct`` / ``listing_max_tokens`` bound only how much of the catalog
gets LISTED, not whether it defers. So no budget knob can fix this; only
``tools.tool_search.enabled: "off"`` can, and it must not be conditional on
anything the delivery path can fail to produce.

This test asserts the contract by executing the real config.yaml seed block out
of ``start.sh`` and parsing what it renders, for BOTH persona states, plus a
source-level ratchet that the stanza is not re-gated. It executes the block
extracted verbatim from ``start.sh`` rather than re-stating the YAML, so a
future edit that drops or re-scopes the stanza fails here instead of silently
shipping.
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


def test_workspace_without_the_concierge_persona_still_gets_tool_search_off(tmp_path):
    """The regression: a box where /configs/prompts/concierge.md never arrived.

    This is the observed staging-tenant-cd / e2e-smoke failure. A concierge whose
    persona asset was not delivered used to render NO ``tools`` key, hermes
    deferred its whole MCP surface, and the A2A provision_workspace turn died on
    ``Model generated invalid tool call: mcp__molecule__get_workspace_info`` —
    a real, registered id from the ``molecule`` sidecar. An ordinary worker is in
    the same position (38 deferred tools measured live). Deferral must never be
    reachable, whatever the asset channel did or did not deliver.
    """
    ts = _tool_search(_render(tmp_path, is_concierge=False))
    assert isinstance(ts, dict), (
        "a workspace WITHOUT /configs/prompts/concierge.md still renders no "
        "tools.tool_search override — hermes will defer its MCP surface and "
        "reject the very tool ids it advertises"
    )
    assert str(ts.get("enabled")).strip().lower() in ("off", "false", "0", "no"), (
        f"tools.tool_search.enabled={ts.get('enabled')!r} does not disable deferral; "
        "hermes falls back to 'auto' for unrecognised values"
    )


def test_tool_search_off_is_not_gated_on_a_delivered_asset():
    """Source ratchet: the stanza must not be re-scoped behind IS_CONCIERGE.

    Re-introducing the persona-file gate is the exact mutation this test exists
    to kill — it is how the fatal state was reachable in the first place.
    """
    src = START_SH.read_text(encoding="utf-8")
    # Comments may (and do) explain the retired gate — only EXECUTABLE lines
    # matter, so strip whole-line comments before looking.
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln for ln in code if "IS_CONCIERGE" in ln]
    assert not offenders, (
        "start.sh still references IS_CONCIERGE in executable code — tool-search "
        "deferral must not be gated on /configs/prompts/concierge.md (or any "
        f"other delivered asset); the control plane does not guarantee it: {offenders}"
    )
    assert re.search(r"^\s*echo \"tools:\"", src, re.M), (
        "the tools/tool_search stanza is missing from the config.yaml seed"
    )


@pytest.mark.parametrize("is_concierge", [True, False])
def test_seed_block_still_renders_parsable_yaml(tmp_path, is_concierge):
    """The stanza must not corrupt the rest of the seeded config."""
    out_dir = tmp_path / str(is_concierge)
    out_dir.mkdir()
    cfg = _render(out_dir, is_concierge=is_concierge)
    assert cfg.get("model", {}).get("default") == "minimax/MiniMax-M2.7"
    assert "mcp_servers" in cfg, "the molecule MCP seed must survive"
