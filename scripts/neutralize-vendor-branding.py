#!/usr/bin/env python3
"""Neutralize the upstream agent's THIRD-PARTY self-identification.

WHY THIS EXISTS (customer-visible defect, 2026-08-05)
-----------------------------------------------------
The vendored upstream hermes-agent builds its system prompt in
``agent/system_prompt.py``. Right after the identity block it does, with no
config knob and no condition whatsoever::

    # Pointer to the hermes-agent skill + docs for user questions about Hermes itself.
    stable_parts.append(HERMES_AGENT_HELP_GUIDANCE)

and ``agent/prompt_builder.py`` defines that constant as::

    "You run on Hermes Agent (by Nous Research). ... the documentation at
     https://hermes-agent.nousresearch.com/docs is your authoritative
     reference ..."

So EVERY workspace on this template — concierge or plain, persona delivered
or not — carries a paragraph in the STABLE tier of its system prompt telling
it that it is a third-party vendor's product. Customers then get greeted with
"Hi — I'm Hermes, a self-improving AI agent from Nous Research."

PAIRED LIVE EVIDENCE (both containers on image 0408b7cdfe09):
  * ``enteros-ws-test1-…``: ``/configs/prompts/concierge.md`` PRESENT, the
    composed ``SOUL.md`` carries ``# You are test1 Agent — the Org Concierge``
    at line 54 — i.e. the persona IS loaded — and the agent STILL introduces
    itself as Nous Research's product.
  * ``enteros-ws-test2-…``: persona ABSENT, same image, same vendor greeting.
The persona file is therefore NOT what determines this text; the unconditional
``stable_parts.append`` is.

``DEFAULT_AGENT_IDENTITY`` in the same module carries the same vendor
attribution ("You are Hermes Agent, an intelligent AI assistant created by
Nous Research.") and is appended whenever no SOUL.md loads, so it is
neutralized here too.

WHICH COPY IS LIVE (this is the whole ballgame)
-----------------------------------------------
There are TWO copies of ``agent/prompt_builder.py`` in the image and they are
not identical (109195 B vs 101647 B on 0408b7cdfe09):

  1. ``$HERMES_ROOT/agent/prompt_builder.py``                       (checkout)
  2. ``$HERMES_ROOT/venv/lib/python3.11/site-packages/agent/prompt_builder.py``
     (the PyPI wheel the Dockerfile pins via ``hermes-agent==${HERMES_VERSION}``)

The gateway imports **the checkout**, because ``~/.local/bin/hermes`` is not a
symlink to the venv console script — it is a wrapper that reads::

    #!/usr/bin/env bash
    unset PYTHONPATH
    unset PYTHONHOME
    exec "$HERMES_ROOT/venv/bin/python" "$HERMES_ROOT/hermes" "$@"

Running a script at ``$HERMES_ROOT/hermes`` puts ``$HERMES_ROOT`` at
``sys.path[0]``, ahead of site-packages, and ``PYTHONPATH`` is explicitly
cleared so nothing can reorder it. Confirmed live: ``/proc/<gateway>/cmdline``
is exactly that argv pair, and an import inside a throwaway container on the
same image prints
``agent.prompt_builder.__file__ = $HERMES_ROOT/agent/prompt_builder.py``.

Patching the wrong copy is a vacuous fix, so this script does two things:
it rewrites EVERY copy it finds under the hermes root, and then it VERIFIES
by re-importing the module the way the gateway does and asserting the value
the gateway will actually see no longer carries the vendor token.

WHY AN APPENDED OVERRIDE RATHER THAN IN-PLACE SURGERY
------------------------------------------------------
The constants are multi-line implicit-concatenation literals; rewriting them
in place with a regex is exactly the class of edit that silently mangles a
file on the next upstream reflow. Python binds ``from agent.prompt_builder
import HERMES_AGENT_HELP_GUIDANCE`` to the module attribute as it stands when
the module has FINISHED executing, so a re-assignment appended at the end of
the module is the value every importer gets. The original text is left in the
file (untouched, above the marker) so a reviewer can diff upstream drift.

NO BRAND LITERAL
----------------
The replacement text deliberately names NO product and NO vendor. This repo
does not vendor the SDK branding contract
(``molecule-ai-sdk:contracts/branding/branding.contract.json``), the published
SDK wheel does not ship ``gen/python/branding_gen.py``
(``[tool.setuptools.packages.find] include = ["molecule_plugin*",
"molecule_external_workspace*"]``), and the platform injects no
product-name variable into the container — so there is no in-container SSOT to
read a display name from, and hardcoding one here would be exactly the drift
this repo's SSOT rules forbid. Pointing the agent at its OWN role definition
is both brand-free and more correct than any product pointer would be.

FAIL LOUD
---------
If an anchor is gone (upstream renamed/moved/reworded the constant), this
script prints an ERROR naming the concrete consequence — the agent will tell
users it runs on Nous Research's product — and exits ``EXIT_ANCHOR_MISSING``.
It NEVER raises to the point of killing the boot; ``start.sh`` logs the failure
and carries on, because a workspace that cannot boot is worse than one with a
wrong byline. ``tests/test_vendor_branding_neutralized.py`` fails on the same
condition so the anchor loss is caught in CI, not in front of a customer.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

# Exit codes. 0 = every anchor found and neutralized (and, when an interpreter
# was available, verified). Anything else is a LOUD failure that start.sh logs.
EXIT_OK = 0
EXIT_ANCHOR_MISSING = 3
EXIT_VERIFY_FAILED = 4
EXIT_NO_MODULE = 5

MARKER = "# --- workspace-template: upstream vendor-identity neutralization ---"
END_MARKER = "# --- end vendor-identity neutralization ---"

# Every token whose presence in an assembled system prompt means the workspace
# is attributing itself to the upstream vendor. Used both to prove the anchor
# is the string we think it is (a non-vacuity precondition: we refuse to
# "neutralize" a constant that was not carrying vendor attribution in the first
# place) and to verify the post-state.
VENDOR_TOKENS = ("Nous Research", "nousresearch.com")

# The replacement keeps upstream's INTENT (where to look for authority when the
# user asks about the agent itself) and drops only the third-party attribution.
HELP_GUIDANCE_REPLACEMENT = (
    "When the user needs help with this workspace itself — configuring, setting "
    "up, using, extending, or troubleshooting it — or when you need to understand "
    "your own features, tools, or capabilities, your own role definition "
    "(SOUL.md), the workspace configuration under /configs, and the operator's "
    "own documentation are your authoritative references. Do not describe "
    "yourself as, or attribute this workspace to, any third-party agent product "
    "or vendor, and do not refer the user to a third-party product's "
    "documentation for questions about this workspace."
)

# Byte-identical to upstream's DEFAULT_AGENT_IDENTITY with the vendor
# attribution sentence replaced; every behavioural clause is preserved so this
# is a branding change and not a behaviour change.
DEFAULT_IDENTITY_REPLACEMENT = (
    "You are the AI agent for this workspace. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

# (module relative path, constant name, replacement).
TARGETS = (
    ("agent/prompt_builder.py", "HERMES_AGENT_HELP_GUIDANCE", HELP_GUIDANCE_REPLACEMENT),
    ("agent/prompt_builder.py", "DEFAULT_AGENT_IDENTITY", DEFAULT_IDENTITY_REPLACEMENT),
)

# What a reader of the log needs to understand, stated in customer terms.
CONSEQUENCE = (
    "the agent will tell users it runs on Nous Research's Hermes Agent product "
    "and point them at nousresearch.com docs"
)


def log(msg: str) -> None:
    print(f"[neutralize-vendor-branding] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[neutralize-vendor-branding] ERROR {msg}", file=sys.stderr, flush=True)


def find_module_copies(root: Path, rel: str) -> list[Path]:
    """Every copy of ``rel`` under ``root``.

    Both the installer's checkout tree and the venv's site-packages carry
    ``agent/prompt_builder.py``; which one wins is a ``sys.path`` question that
    a future launcher change could flip. Rewriting every copy makes the fix
    independent of that resolution, and ``verify_live_value`` still proves the
    one the gateway actually imports.
    """
    tail = Path(rel)
    seen: dict[Path, None] = {}
    for candidate in root.rglob(tail.name):
        if candidate.parent.name != tail.parent.name:
            continue
        try:
            key = candidate.resolve()
        except OSError:
            key = candidate
        if key not in seen:
            seen[key] = None
    return sorted(seen)


def read_constant(source: str, name: str) -> str | None:
    """The module-level string value assigned to ``name``, or None.

    Uses ``ast`` rather than a regex: the constants are multi-line implicit
    concatenations, and only the LAST module-level assignment matters (that is
    precisely the semantics this script relies on to override them).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    value: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    literal = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError):
                    return None
                if isinstance(literal, str):
                    value = literal
    return value


def _override_block(pairs: list[tuple[str, str]]) -> str:
    lines = [
        "",
        "",
        MARKER,
        "# Appended by scripts/neutralize-vendor-branding.py at container boot.",
        "# The upstream definitions above are left intact for drift review; the",
        "# re-assignments below are what every importer of this module sees,",
        "# because `from ... import NAME` binds the attribute as it stands after",
        "# the module has finished executing. See the script docstring for the",
        "# customer-visible defect this closes.",
    ]
    for name, replacement in pairs:
        lines.append(f"{name} = {replacement!r}")
    lines.append(END_MARKER)
    lines.append("")
    return "\n".join(lines)


def strip_existing_block(source: str) -> str:
    """Drop a previously appended block so re-running is idempotent.

    ``start.sh`` runs on every container START, not just on create, and the
    container's writable layer persists across ``docker restart`` — without
    this the module would grow an override block per boot.
    """
    start = source.find(MARKER)
    if start == -1:
        return source
    end = source.find(END_MARKER, start)
    if end == -1:
        return source[:start].rstrip("\n") + "\n"
    end += len(END_MARKER)
    return (source[:start].rstrip("\n") + "\n" + source[end:].lstrip("\n")).rstrip("\n") + "\n"


def neutralize_file(path: Path) -> tuple[bool, list[str]]:
    """Rewrite one module copy. Returns (changed, problems)."""
    problems: list[str] = []
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable file is a hard fail
        return False, [f"cannot read {path}: {exc}"]

    base = strip_existing_block(original)
    pairs: list[tuple[str, str]] = []
    for rel, name, replacement in TARGETS:
        if not str(path).replace("\\", "/").endswith(rel):
            continue
        current = read_constant(base, name)
        if current is None:
            problems.append(
                f"{path}: anchor {name} NOT FOUND — upstream renamed, moved or "
                f"restructured it, so this patch is a no-op and {CONSEQUENCE}"
            )
            continue
        if not any(token in current for token in VENDOR_TOKENS):
            # Non-vacuity precondition. If the upstream text no longer carries
            # any vendor token, either upstream fixed it (and this patch is
            # dead weight that should be retired) or we are matching the wrong
            # constant. Either way a human must look; never silently "succeed".
            problems.append(
                f"{path}: anchor {name} carries none of {VENDOR_TOKENS!r} — the "
                "constant this patch was written against has changed meaning; "
                f"the neutralization can no longer be trusted and {CONSEQUENCE} "
                "if the attribution moved elsewhere"
            )
            continue
        pairs.append((name, replacement))

    if not pairs:
        return False, problems

    updated = base.rstrip("\n") + "\n" + _override_block(pairs)
    if updated == original:
        return False, problems
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        problems.append(f"cannot write {path}: {exc}")
        return False, problems
    log(f"neutralized {', '.join(n for n, _ in pairs)} in {path}")
    return True, problems


def verify_live_value(python: Path, hermes_root: Path) -> list[str]:
    """Import the module the way the gateway does and inspect what it got.

    This is the anti-vacuity check. The gateway runs
    ``<venv>/bin/python <hermes_root>/hermes gateway``, so ``sys.path[0]`` is
    ``hermes_root``; reproducing that here proves we patched the copy that is
    actually imported rather than a copy nothing loads.
    """
    probe = (
        "import json, sys\n"
        "import agent.prompt_builder as pb\n"
        "out = {'file': pb.__file__}\n"
        "for name in %r:\n"
        "    out[name] = getattr(pb, name, None)\n"
        "print(json.dumps(out))\n"
    ) % ([name for _, name, _ in TARGETS],)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    try:
        proc = subprocess.run(
            [str(python), "-c", probe],
            cwd=str(hermes_root),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"verification could not run ({exc}); patched value UNPROVEN"]
    if proc.returncode != 0:
        return [
            "verification import failed (rc="
            f"{proc.returncode}); patched value UNPROVEN: "
            f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ''}"
        ]
    import json

    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ["verification produced no parsable output; patched value UNPROVEN"]

    problems = []
    log(f"live import resolves to {out.get('file')}")
    for _, name, _ in TARGETS:
        value = out.get(name)
        if not isinstance(value, str):
            problems.append(f"live module exposes no string {name}; {CONSEQUENCE}")
            continue
        hit = [token for token in VENDOR_TOKENS if token in value]
        if hit:
            problems.append(
                f"live value of {name} STILL contains {hit!r} after patching "
                f"{out.get('file')} — the patch did not reach the imported copy; "
                f"{CONSEQUENCE}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hermes-root",
        required=True,
        help="root of the upstream install (the dir holding agent/ and venv/)",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="interpreter used for the post-patch verification import; "
        "defaults to <hermes-root>/venv/bin/python",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="patch only; do not re-import to prove the live value changed",
    )
    args = parser.parse_args(argv)

    root = Path(args.hermes_root)
    if not root.is_dir():
        err(f"hermes root {root} does not exist — nothing patched; {CONSEQUENCE}")
        return EXIT_NO_MODULE

    problems: list[str] = []
    touched_any = False
    for rel in sorted({rel for rel, _, _ in TARGETS}):
        copies = find_module_copies(root, rel)
        if not copies:
            err(f"no copy of {rel} found under {root} — nothing patched; {CONSEQUENCE}")
            problems.append(f"missing module {rel}")
            continue
        log(f"{rel}: {len(copies)} cop{'y' if len(copies) == 1 else 'ies'} under {root}")
        for copy in copies:
            changed, file_problems = neutralize_file(copy)
            touched_any = touched_any or changed
            problems.extend(file_problems)

    if not touched_any and not problems:
        log("already neutralized (idempotent re-run)")

    for problem in problems:
        err(problem)

    if problems:
        # Partial success is still failure: a single unpatched anchor is enough
        # to put a third-party byline in front of a customer.
        return EXIT_ANCHOR_MISSING

    if args.skip_verify:
        log("verification skipped by request")
        return EXIT_OK

    python = Path(args.python) if args.python else root / "venv" / "bin" / "python"
    if not python.exists():
        err(
            f"verification interpreter {python} is missing — the patch was "
            "written but NOT proven to reach the module the gateway imports"
        )
        return EXIT_VERIFY_FAILED

    verify_problems = verify_live_value(python, root)
    for problem in verify_problems:
        err(problem)
    if verify_problems:
        return EXIT_VERIFY_FAILED

    log("verified: the imported module carries no vendor self-attribution")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
