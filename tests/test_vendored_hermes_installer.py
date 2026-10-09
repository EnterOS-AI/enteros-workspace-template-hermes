"""The hermes installer is vendored, checksum-verified, and never piped.

On 2026-09-29 every image build of the runtime 0.4.92 bump failed at
`RUN curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash -s -- ...`:
raw.githubusercontent.com answered HTTP 429, the RUN had no pipefail, bash ran
an empty script and exited 0, and the build died one step later with
"fatal: cannot change to '/home/agent/.hermes/hermes-agent'".

The Dockerfile now runs vendor/hermes-agent/install.sh after checking its
sha256. These tests hold that shape offline. The CI job gateway-live-roundtrip
compares the copy with scripts/install.sh at HERMES_COMMIT upstream.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
VENDORED = ROOT / "vendor" / "hermes-agent" / "install.sh"
PROVENANCE = ROOT / "vendor" / "hermes-agent" / "README.md"
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"

# A download command whose output feeds a pipe (`|`, not `||`) in the same
# simple command.
PIPED_DOWNLOAD = re.compile(r"\b(?:curl|wget)\b[^|;&]*\|(?!\|)")


def _instructions() -> list[str]:
    """Dockerfile instructions, comments dropped, continuation lines joined."""
    kept = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    joined = re.sub(r"\\\n", " ", "\n".join(kept))
    return [line.strip() for line in joined.splitlines() if line.strip()]


def _arg(name: str) -> str:
    match = re.search(
        rf"^ARG {name}=(\S+)$", DOCKERFILE.read_text(encoding="utf-8"), re.M
    )
    assert match, f"the Dockerfile no longer declares ARG {name}"
    return match.group(1)


def _git_blob_id(data: bytes) -> str:
    """What `git hash-object` prints for a file with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _installer_run() -> tuple[str, str]:
    """(COPY destination, the RUN that executes it)."""
    instructions = _instructions()
    copies = [
        line
        for line in instructions
        if line.startswith("COPY") and "vendor/hermes-agent/install.sh" in line.split()
    ]
    assert len(copies) == 1, (
        "the Dockerfile must COPY vendor/hermes-agent/install.sh exactly once; "
        f"found {copies}"
    )
    destination = copies[0].split()[-1]
    runs = [
        line
        for line in instructions
        if line.startswith("RUN") and f"bash {destination}" in line
    ]
    assert len(runs) == 1, (
        f"expected exactly one RUN executing `bash {destination}`; found {runs}"
    )
    return destination, runs[0]


def test_no_run_pipes_a_download():
    """A failed download must fail its own step, not the next one."""
    offenders = [
        line
        for line in _instructions()
        if line.startswith("RUN")
        and PIPED_DOWNLOAD.search(line)
        and "pipefail" not in line
    ]
    assert offenders == [], (
        "RUN pipes a download without pipefail; the pipeline's status is the "
        "last command's, so a failed fetch (for example an HTTP 429) exits 0 "
        f"here and fails a later step instead: {offenders}"
    )


def test_installer_runs_from_the_vendored_copy_after_its_checksum():
    destination, run = _installer_run()
    assert "raw.githubusercontent.com" not in run, (
        "the installer step downloads again; it must run the vendored copy"
    )
    check = run.find("sha256sum -c")
    execute = run.find(f"bash {destination}")
    assert 0 <= check < execute, (
        "the installer step must verify the vendored copy with `sha256sum -c` "
        f"before it runs it: {run}"
    )
    assert "${HERMES_INSTALL_SH_SHA256}" in run[:execute], (
        "the checksum verified before the installer runs must be "
        f"ARG HERMES_INSTALL_SH_SHA256: {run}"
    )


def test_installer_arguments_are_unchanged():
    """Vendoring changes where the script comes from, not what it installs."""
    destination, run = _installer_run()
    assert (
        f'bash {destination} --skip-setup --commit "${{HERMES_COMMIT}}" '
        "--force-commit" in run
    ), f"installer arguments changed: {run}"


def test_pinned_digest_is_the_vendored_copy():
    expected = _arg("HERMES_INSTALL_SH_SHA256")
    assert re.fullmatch(r"[0-9a-f]{64}", expected), (
        f"ARG HERMES_INSTALL_SH_SHA256 is not a sha256 hex digest: {expected}"
    )
    actual = hashlib.sha256(VENDORED.read_bytes()).hexdigest()
    assert actual == expected, (
        f"vendor/hermes-agent/install.sh hashes to {actual}, but the Dockerfile "
        f"pins {expected}; the image build would fail at `sha256sum -c`"
    )


def test_vendored_copy_is_recorded_against_the_pinned_commit():
    """Moving HERMES_COMMIT without refreshing the installer turns this red."""
    provenance = PROVENANCE.read_text(encoding="utf-8")
    commit = re.search(r"^upstream_commit: ([0-9a-f]{40})$", provenance, re.M)
    blob = re.search(r"^git_blob: ([0-9a-f]{40})$", provenance, re.M)
    assert commit and blob, (
        "vendor/hermes-agent/README.md must record upstream_commit and git_blob"
    )
    assert commit.group(1) == _arg("HERMES_COMMIT"), (
        f"the vendored installer was taken at {commit.group(1)} but the "
        f"Dockerfile pins HERMES_COMMIT={_arg('HERMES_COMMIT')}; refresh it "
        "(vendor/hermes-agent/README.md)"
    )
    actual = _git_blob_id(VENDORED.read_bytes())
    assert actual == blob.group(1), (
        f"vendor/hermes-agent/install.sh is blob {actual}, not the recorded "
        f"upstream blob {blob.group(1)}"
    )


def test_ci_compares_the_vendored_copy_with_upstream_at_the_pin():
    """The offline tests cannot see upstream; the required CI job does."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    steps = [
        str(step.get("run", ""))
        for step in jobs["gateway-live-roundtrip"].get("steps", [])
    ]
    assert any(
        'rev-parse "$HERMES_COMMIT:scripts/install.sh"' in script
        and "git hash-object vendor/hermes-agent/install.sh" in script
        for script in steps
    ), "gateway-live-roundtrip no longer compares the vendored installer with upstream"
    for aggregate in ("validate", "all-required"):
        assert "gateway-live-roundtrip" in jobs[aggregate]["needs"], (
            f"{aggregate} no longer requires gateway-live-roundtrip"
        )
