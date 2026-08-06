"""The assembled system prompt must not attribute the workspace to a vendor.

Why this test exists
--------------------
The vendored upstream hermes-agent appends a THIRD-PARTY self-description into
the STABLE tier of every workspace's system prompt, unconditionally. In
``agent/system_prompt.py`` (0.19.0, line ~204)::

    # Pointer to the hermes-agent skill + docs for user questions about Hermes itself.
    stable_parts.append(HERMES_AGENT_HELP_GUIDANCE)

with no config knob and no guard, immediately after the identity block; and
``agent/prompt_builder.py`` defines::

    HERMES_AGENT_HELP_GUIDANCE = (
        "You run on Hermes Agent (by Nous Research). ... the documentation at "
        "https://hermes-agent.nousresearch.com/docs is your authoritative "
        "reference ..."
    )

Measured live on two containers running the SAME image (0408b7cdfe09),
differing only in whether the persona asset was delivered:

  * ``enteros-ws-test1-…`` — ``/configs/prompts/concierge.md`` PRESENT, the
    composed ``SOUL.md`` carries ``# You are test1 Agent — the Org Concierge``
    at line 54, so the persona IS loaded — and the agent still introduces
    itself as "a self-improving AI agent from Nous Research";
  * ``enteros-ws-test2-…`` — persona ABSENT, same image, same vendor greeting.

Same text on both, so the persona file is not what produces it. The
unconditional ``stable_parts.append`` is.

What this file asserts
----------------------
It runs the REAL ``scripts/neutralize-vendor-branding.py`` against a fixture
package that reproduces upstream's exact constants and module layout, then
IMPORTS the patched module the way the gateway does and inspects the value the
importer actually receives. It does not restate the patch logic, and it does
not assert on a hardcoded file path — it follows the real ``sys.path``
resolution, because two copies of ``agent/prompt_builder.py`` exist in the
image (checkout tree and venv site-packages) and a launcher change could flip
which one wins.

Non-vacuity precondition
------------------------
``test_fixture_reproduces_the_defect_before_patching`` asserts the UNPATCHED
fixture really does expose the vendor attribution. Without it, every
"neutralized" assertion below could pass against a fixture that never carried
the string in the first place.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "neutralize-vendor-branding.py"
START_SH = REPO / "start.sh"

# Verbatim from the upstream module the image ships (hermes-agent 0.19.0,
# /home/agent/.hermes/hermes-agent/agent/prompt_builder.py lines 144-166).
# Copied byte-for-byte so the fixture fails the same way production does.
UPSTREAM_PROMPT_BUILDER = '''\
"""Fixture standing in for upstream agent/prompt_builder.py."""

DEFAULT_AGENT_IDENTITY = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

HERMES_AGENT_HELP_GUIDANCE = (
    "You run on Hermes Agent (by Nous Research). When the user needs help with "
    "Hermes itself — configuring, setting up, using, extending, or troubleshooting "
    "it — or when you need to understand your own features, tools, or capabilities, "
    "the documentation at https://hermes-agent.nousresearch.com/docs is your "
    "authoritative reference and always holds the latest, most up-to-date "
    "information. Load the `hermes-agent` skill with skill_view(name='hermes-agent') "
    "for additional guidance and proven workflows, but treat the docs as the source "
    "of truth when the two differ."
)

MEMORY_GUIDANCE = "You have persistent memory across sessions."
'''

# Reproduces the unconditional append that puts the constant in the prompt.
UPSTREAM_SYSTEM_PROMPT = '''\
"""Fixture standing in for upstream agent/system_prompt.py."""

from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    HERMES_AGENT_HELP_GUIDANCE,
)


def build_stable_parts(soul_content=None):
    stable_parts = []
    if soul_content:
        stable_parts.append(soul_content)
    else:
        stable_parts.append(DEFAULT_AGENT_IDENTITY)
    # Pointer to the hermes-agent skill + docs for user questions about Hermes itself.
    stable_parts.append(HERMES_AGENT_HELP_GUIDANCE)
    return stable_parts
'''

# Tokens that mean "this workspace just told the customer it is a third party's
# product". Kept in sync with the script's own VENDOR_TOKENS by
# ``test_vendor_token_list_matches_the_script``.
VENDOR_TOKENS = ("Nous Research", "nousresearch.com")

# The one thing this repo must never do to satisfy the fix: hardcode a product
# name. The SDK branding contract is the SSOT and this repo does not vendor it.
BRAND_LITERALS = ("Enter OS", "Enter OS Server")


def _fixture_root(tmp_path: pathlib.Path, *, with_venv_copy: bool = True) -> pathlib.Path:
    """A minimal stand-in for /home/agent/.hermes/hermes-agent.

    Includes BOTH copies of ``agent/prompt_builder.py`` that the real image
    carries — the checkout tree and the venv site-packages install — because
    patching only the one nothing imports is the exact vacuous fix this guard
    exists to prevent.
    """
    root = tmp_path / "hermes-agent"
    checkout = root / "agent"
    checkout.mkdir(parents=True)
    (checkout / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "prompt_builder.py").write_text(UPSTREAM_PROMPT_BUILDER, encoding="utf-8")
    (checkout / "system_prompt.py").write_text(UPSTREAM_SYSTEM_PROMPT, encoding="utf-8")
    if with_venv_copy:
        site = root / "venv" / "lib" / "python3.11" / "site-packages" / "agent"
        site.mkdir(parents=True)
        (site / "__init__.py").write_text("", encoding="utf-8")
        (site / "prompt_builder.py").write_text(UPSTREAM_PROMPT_BUILDER, encoding="utf-8")
        (site / "system_prompt.py").write_text(UPSTREAM_SYSTEM_PROMPT, encoding="utf-8")
    return root


def _run_script(root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--hermes-root",
            str(root),
            "--python",
            sys.executable,
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def _assembled_prompt(root: pathlib.Path) -> tuple[str, str]:
    """Import the fixture the way the gateway does and build the stable tier.

    ``sys.path[0]`` is the hermes root because that is what running
    ``<venv>/bin/python <hermes-root>/hermes gateway`` produces — the wrapper
    at ``~/.local/bin/hermes`` execs exactly that argv pair and clears
    ``PYTHONPATH``/``PYTHONHOME`` first, so nothing can reorder it.

    Returns ``(prompt_text, resolved_module_file)``. The resolved file is
    returned rather than asserted against a constant so the guard follows the
    real resolution instead of pinning a path that could go stale.
    """
    probe = textwrap.dedent(
        """
        import json, sys
        import agent.prompt_builder as pb
        from agent.system_prompt import build_stable_parts
        print(json.dumps({
            "prompt": "\\n\\n".join(build_stable_parts(soul_content=None)),
            "prompt_with_soul": "\\n\\n".join(
                build_stable_parts(soul_content="# You are test1 Agent — the Org Concierge")
            ),
            "file": pb.__file__,
        }))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload, payload["file"]


# --------------------------------------------------------------------------
# Non-vacuity precondition
# --------------------------------------------------------------------------


def test_fixture_reproduces_the_defect_before_patching(tmp_path):
    """The UNPATCHED fixture must carry the vendor attribution.

    Everything below is meaningless if the fixture never had the string. This
    is the precondition that makes the rest of the file a real guard rather
    than a pass-by-construction.
    """
    root = _fixture_root(tmp_path)
    payload, resolved = _assembled_prompt(root)
    assert resolved == str(root / "agent" / "prompt_builder.py"), (
        "fixture resolution does not match the gateway's (script dir first); "
        f"resolved {resolved}"
    )
    for key in ("prompt", "prompt_with_soul"):
        assert any(token in payload[key] for token in VENDOR_TOKENS), (
            f"fixture[{key}] carries no vendor token — the fixture no longer "
            "reproduces the production defect and every assertion below is vacuous"
        )


# --------------------------------------------------------------------------
# The fix
# --------------------------------------------------------------------------


def test_neutralizer_removes_vendor_attribution_from_the_assembled_prompt(tmp_path):
    """The customer-visible contract: no vendor byline, persona or not.

    Both branches matter — test1 (persona delivered) and test2 (persona
    absent) said the same thing on the same image, which is what proved the
    persona file was not the cause.
    """
    root = _fixture_root(tmp_path)
    proc = _run_script(root)
    assert proc.returncode == 0, f"neutralizer failed: {proc.stdout}\n{proc.stderr}"

    payload, resolved = _assembled_prompt(root)
    assert resolved.endswith("prompt_builder.py")
    for key in ("prompt", "prompt_with_soul"):
        offenders = [token for token in VENDOR_TOKENS if token in payload[key]]
        assert not offenders, (
            f"the assembled system prompt still contains {offenders!r} in "
            f"{key} — every customer of this workspace is told it runs on a "
            "third party's product"
        )


def test_neutralizer_patches_the_copy_that_is_actually_imported(tmp_path):
    """Anti-vacuity: the patched copy must be the one ``sys.path`` resolves.

    The image ships two non-identical copies (109195 B checkout vs 101647 B
    venv site-packages on 0408b7cdfe09). A patch applied to the copy nothing
    imports changes nothing.
    """
    root = _fixture_root(tmp_path)
    assert _run_script(root).returncode == 0

    _, resolved = _assembled_prompt(root)
    text = pathlib.Path(resolved).read_text(encoding="utf-8")
    assert "workspace-template: upstream vendor-identity neutralization" in text, (
        f"the module actually imported ({resolved}) was NOT patched — the fix "
        "landed on a copy nothing loads"
    )


def test_neutralizer_is_idempotent_across_reboots(tmp_path):
    """``start.sh`` runs on every container START, not only on create.

    The writable layer survives ``docker restart``, so a non-idempotent
    appender would grow the module by one override block per boot.
    """
    root = _fixture_root(tmp_path)
    assert _run_script(root).returncode == 0
    live = root / "agent" / "prompt_builder.py"
    once = live.read_text(encoding="utf-8")
    assert _run_script(root).returncode == 0
    twice = live.read_text(encoding="utf-8")
    assert once == twice, "re-running the neutralizer changed the file again"
    assert twice.count("upstream vendor-identity neutralization ---") == 1


# --------------------------------------------------------------------------
# Fail-loud on upstream drift
# --------------------------------------------------------------------------


def test_missing_anchor_fails_loudly_and_names_the_consequence(tmp_path):
    """A future upstream bump must not silently no-op back to vendor branding.

    This is the regression that the whole design is aimed at: a patch that
    quietly matches nothing looks identical to a patch that worked.
    """
    root = _fixture_root(tmp_path)
    live = root / "agent" / "prompt_builder.py"
    # Upstream renames the constant — the exact drift that would make a
    # sed-style patch a silent no-op.
    live.write_text(
        UPSTREAM_PROMPT_BUILDER.replace(
            "HERMES_AGENT_HELP_GUIDANCE", "AGENT_SELF_HELP_GUIDANCE"
        ),
        encoding="utf-8",
    )
    proc = _run_script(root)
    assert proc.returncode != 0, (
        "a missing anchor exited 0 — the boot would look healthy while the "
        "agent kept telling customers it runs on Nous Research's product"
    )
    combined = proc.stdout + proc.stderr
    assert "ERROR" in combined
    assert "HERMES_AGENT_HELP_GUIDANCE" in combined, combined
    assert "Nous Research" in combined, (
        "the failure message does not name the concrete consequence: " + combined
    )


def test_anchor_that_lost_its_vendor_token_fails_loudly(tmp_path):
    """A constant that no longer carries the vendor text is not a green light.

    Either upstream fixed it (retire this patch) or we are now matching a
    different constant (the attribution moved). Both need a human.
    """
    root = _fixture_root(tmp_path)
    live = root / "agent" / "prompt_builder.py"
    live.write_text(
        UPSTREAM_PROMPT_BUILDER.replace("Hermes Agent (by Nous Research)", "this agent")
        .replace("https://hermes-agent.nousresearch.com/docs", "the local docs")
        .replace("created by Nous Research", "for this workspace"),
        encoding="utf-8",
    )
    proc = _run_script(root)
    assert proc.returncode != 0, (
        "the neutralizer reported success against a constant that carried no "
        "vendor token — it can no longer tell 'patched' from 'wrong target'"
    )
    assert "ERROR" in proc.stdout + proc.stderr


def test_missing_module_fails_loudly(tmp_path):
    """A relocated upstream tree must not read as a clean run."""
    root = tmp_path / "hermes-agent"
    (root / "somewhere-else").mkdir(parents=True)
    proc = _run_script(root)
    assert proc.returncode != 0
    assert "ERROR" in proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# Wiring + SSOT ratchets
# --------------------------------------------------------------------------


def test_start_sh_actually_invokes_the_neutralizer():
    """A guard that ships but is never called is not a guard.

    Asserts on EXECUTABLE lines only, so a comment mentioning the script
    cannot satisfy it.
    """
    src = START_SH.read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    calls = [ln for ln in code if "neutralize-vendor-branding.py" in ln]
    assert calls, (
        "start.sh never runs scripts/neutralize-vendor-branding.py — the "
        "upstream vendor attribution stays in every workspace's system prompt"
    )


def test_dockerfile_ships_the_neutralizer_into_the_image():
    """``COPY scripts/`` must still be what puts the script in the image."""
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY scripts/ /app/scripts/$", dockerfile, re.M), (
        "the Dockerfile no longer copies scripts/ into the image; start.sh "
        "would fail to find the neutralizer at boot"
    )


def test_no_product_brand_literal_is_hardcoded():
    """The product display name is SDK SSOT, never a literal in this repo.

    ``molecule-ai-sdk:contracts/branding/branding.contract.json`` owns
    ``product_display_name``. This repo does not vendor that contract and the
    published SDK wheel does not ship ``gen/python/branding_gen.py``, so there
    is no in-repo or in-container source to read it from — which is exactly why
    the replacement text names no product at all. Hardcoding the display name
    here would create silent drift on the next rebrand.
    """
    offenders = []
    for path in (SCRIPT, START_SH):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            for literal in BRAND_LITERALS:
                if literal in line:
                    offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        "a product brand literal is hardcoded; derive it from the SDK branding "
        f"contract or name no product at all: {offenders}"
    )


# The upstream release whose agent/prompt_builder.py the fixture above was
# copied from, and against which the neutralizer was verified end-to-end inside
# a throwaway container on image 0408b7cdfe09. Bumping the pin without
# re-checking the anchor is exactly how this patch would silently become a
# no-op, so the bump has to come through here.
VERIFIED_HERMES_VERSION = "0.19.0"


def test_upstream_pin_matches_the_version_this_patch_was_verified_against():
    """An upstream bump must not silently carry the patch past its anchor.

    The fixture in this file is a byte copy of ``agent/prompt_builder.py``'s
    constants at ``HERMES_VERSION`` below. A fixture-only guard cannot notice
    upstream renaming the constant in a LATER release — the fixture would keep
    passing while the real image reverted to the vendor byline (the boot-time
    ERROR would fire, but only in production logs). Tying the guard to the pin
    turns the daily upstream-sync bump PR red until a human re-reads
    ``prompt_builder.py`` and re-captures the fixture.
    """
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ARG HERMES_VERSION=(\S+)$", dockerfile, re.M)
    assert m, "ARG HERMES_VERSION is gone from the Dockerfile"
    assert m.group(1) == VERIFIED_HERMES_VERSION, (
        f"Dockerfile pins hermes-agent {m.group(1)} but the vendor-branding "
        f"neutralizer was only verified against {VERIFIED_HERMES_VERSION}. "
        "Re-read agent/prompt_builder.py in the new release: confirm "
        "HERMES_AGENT_HELP_GUIDANCE and DEFAULT_AGENT_IDENTITY still exist and "
        "still carry the vendor attribution, re-capture UPSTREAM_PROMPT_BUILDER "
        "in this file, then bump VERIFIED_HERMES_VERSION. If the anchor moved "
        "and this is skipped, every workspace goes back to telling customers it "
        "runs on Nous Research's product."
    )


def test_vendor_token_list_matches_the_script():
    """Keep the test's notion of "vendor token" tied to the script's."""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^VENDOR_TOKENS = \((.*?)\)$", src, re.M | re.S)
    assert m, "VENDOR_TOKENS not found in the neutralizer"
    found = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert found == VENDOR_TOKENS, (
        f"script VENDOR_TOKENS {found} drifted from this test's {VENDOR_TOKENS}"
    )


# --------------------------------------------------------------------------
# start.sh: the silent persona-graft miss
# --------------------------------------------------------------------------

# The persona graft is the `for persona in … done` loop, optionally preceded by
# its `persona_grafted=0` seed and followed by the column-0 `if … fi` that
# reports a miss. The loop delimiters are the anchor — a moved or renamed loop
# fails loudly here. The trailing branch is matched OPTIONALLY on purpose: if
# someone deletes only the else branch, this still extracts a runnable block and
# the test fails with the message that explains WHY that branch exists, instead
# of degrading into an unhelpful "anchor not found".
_LOOP_RE = re.compile(
    r"^(?:persona_grafted=0\n)?for persona in .*?^done$", re.M | re.S
)
_MISS_BRANCH_RE = re.compile(r"\A\nif \[ .*?^fi$", re.M | re.S)


def _graft_block() -> str:
    src = START_SH.read_text(encoding="utf-8")
    m = _LOOP_RE.search(src)
    assert m, "could not locate the persona->SOUL.md graft loop in start.sh"
    block = m.group(0)
    tail = _MISS_BRANCH_RE.match(src[m.end():])
    if tail:
        block += tail.group(0)
    return block


def _run_graft(tmp_path: pathlib.Path, *, personas: dict[str, str]) -> subprocess.CompletedProcess:
    """Execute start.sh's REAL graft loop against a fake /configs tree.

    Runs the block extracted verbatim rather than restating it, so an edit that
    drops the else branch fails here instead of shipping silently. ``install``
    is shimmed because the real block chowns to the `agent` user, which does
    not exist on a CI runner.
    """
    configs = tmp_path / "configs"
    (configs / "prompts").mkdir(parents=True)
    for rel, body in personas.items():
        (configs / rel).write_text(body, encoding="utf-8")
    home = tmp_path / "hermes-home"
    home.mkdir()

    block = _graft_block()
    block = block.replace("/configs/", f"{configs}/")
    script = "\n".join(
        [
            "set -uo pipefail",
            f'HERMES_HOME="{home}"',
            # install(1) shim: drop the ownership flags CI cannot satisfy.
            "install() { : ; args=(); for a in \"$@\"; do case \"$a\" in "
            "-o|-g) shift_next=1 ;; *) if [ \"${shift_next:-0}\" = 1 ]; then "
            "shift_next=0; else args+=(\"$a\"); fi ;; esac; done; "
            "command install \"${args[@]}\"; }",
            block,
        ]
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_graft_still_installs_a_delivered_persona(tmp_path):
    """Non-vacuity for the loop: the happy path must still work."""
    proc = _run_graft(tmp_path, personas={"prompts/concierge.md": "# You are test1 Agent"})
    assert proc.returncode == 0, proc.stderr
    assert "grafted persona" in proc.stdout, proc.stdout + proc.stderr
    assert (tmp_path / "hermes-home" / "SOUL.md").read_text(encoding="utf-8").startswith(
        "# You are test1 Agent"
    )


def test_missing_persona_is_reported_loudly(tmp_path):
    """The silent degradation that put a third-party identity in front of a customer.

    ``enteros-ws-test2-…`` boots with no ``/configs/prompts/`` at all. Today the
    loop just falls off the end: no persona, no message, nothing in
    ``docker logs`` to distinguish it from a healthy boot. The workspace must
    still BOOT (a workspace that cannot boot is worse), but the miss has to be
    visible.
    """
    proc = _run_graft(tmp_path, personas={})
    assert proc.returncode == 0, (
        "a missing persona must not fail the boot: " + proc.stderr
    )
    combined = proc.stdout + proc.stderr
    assert "[start.sh] ERROR" in combined, (
        "no persona file was delivered and start.sh said NOTHING — this is the "
        "exact silent degradation that shipped a stock third-party identity to "
        "a customer:\n" + combined
    )
    assert "persona" in combined.lower()
    assert not (tmp_path / "hermes-home" / "SOUL.md").exists(), (
        "the else branch must only LOG; it must not fabricate a SOUL.md"
    )


@pytest.mark.parametrize("empty", [True, False])
def test_zero_byte_persona_counts_as_missing(tmp_path, empty):
    """``[ -s ]`` treats a 0-byte persona as absent; the log must agree."""
    body = "" if empty else "# You are test1 Agent"
    proc = _run_graft(tmp_path, personas={"prompts/concierge.md": body})
    combined = proc.stdout + proc.stderr
    if empty:
        assert "[start.sh] ERROR" in combined, combined
    else:
        assert "[start.sh] ERROR" not in combined, combined


def test_start_sh_is_still_valid_bash():
    proc = subprocess.run(["bash", "-n", str(START_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
