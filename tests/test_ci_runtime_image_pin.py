"""Contract checks for exact runtime provenance in pull-request image builds."""

import hashlib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"
META_CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "meta-ci-advisory.yml"
FORK_RUN = "github.event.pull_request.head.repo.fork != true"
FORK_SKIP = "github.event.pull_request.head.repo.fork == true"
MOLECULE_CI_REF = "".join(("11b8598e5c0b3f0b1031", "733a8d5f6bc238f146a4"))
# SHA-256 of templates/ci-meta.yml at MOLECULE_CI_REF.
META_CI_SHA256 = "24bae0ffc8e6cae1b5b3fdc1b7c80640796cfc8c8d5165bef2baad2831661937"
T4_TAG_ASSIGNMENT = (
    'T4_TAG="t4-conformance-test:${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"'
)


def _docker_build_script(job_name: str) -> str:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    scripts = [
        step.get("run", "")
        for step in jobs[job_name]["steps"]
        if "docker build" in step.get("run", "")
    ]
    assert len(scripts) == 1, f"expected one docker build in {job_name}"
    return scripts[0]


def _named_step(job_name: str, step_name: str) -> dict:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    matches = [
        step for step in jobs[job_name]["steps"] if step.get("name") == step_name
    ]
    assert len(matches) == 1, f"expected one {step_name!r} step in {job_name}"
    return matches[0]


@pytest.mark.parametrize("job_name", ("validate-runtime", "t4-conformance"))
def test_pr_image_build_pins_and_verifies_exact_runtime(job_name: str) -> None:
    script = _docker_build_script(job_name)

    assert '--build-arg RUNTIME_VERSION="$EXPECTED_RUNTIME_VERSION"' in script
    assert "importlib.metadata import version" in script
    assert 'version("molecules-workspace-runtime")' in script
    assert '"$ACTUAL_RUNTIME_VERSION" != "$EXPECTED_RUNTIME_VERSION"' in script
    if job_name == "validate-runtime":
        assert ".runtime-version" in script
        assert (
            'SMOKE_TAG="molecule-ai-workspace-hermes-smoke-'
            '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in script
        )
        assert '-t "$SMOKE_TAG"' in script
        assert 'docker run --rm --entrypoint python3 "$SMOKE_TAG"' in script
        assert "template-test" not in script
    else:
        assert 'ATTESTATION="$RUNNER_TEMP/mcp-pin-attestation-' in script
        assert "load_attestation" in script
        assert "json.load" not in script
        assert "re.fullmatch" not in script
        assert T4_TAG_ASSIGNMENT in script


def test_t4_fetches_exact_molecule_ci_and_generates_attestation() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    t4_job = jobs["t4-conformance"]
    fetch_step = _named_step(
        "t4-conformance", "Fetch immutable molecule-ci MCP proof tools"
    )
    script = fetch_step["run"]

    assert t4_job["env"]["MOLECULE_CI_REF"] == MOLECULE_CI_REF
    assert fetch_step["if"] == FORK_RUN
    assert "https://git.moleculesai.app/molecule-ai/molecule-ci.git" in script
    assert "GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0" in script
    assert "-c credential.helper= -c http.userAgent=curl/8.4.0" in script
    assert '-C "$CI_ROOT" fetch -q --no-tags --depth 1' in script
    assert 'origin "$MOLECULE_CI_REF"' in script
    assert 'test "$(git -C "$CI_ROOT" rev-parse HEAD)" = "$MOLECULE_CI_REF"' in script
    assert 'mcp_pin_lockstep.py" --repo-root . --json' in script
    assert 'test -f "$VERIFIER"' in script
    assert "Authorization" not in script


def test_t4_seals_reviewed_tools_and_attestation_at_each_execution_boundary() -> None:
    fetch = _named_step(
        "t4-conformance", "Fetch immutable molecule-ci MCP proof tools"
    )["run"]
    build = _named_step("t4-conformance", "Build the runtime image")["run"]
    verify = _named_step("t4-conformance", "Verify management MCP in the final image")[
        "run"
    ]
    git_seal = (
        'git -C "$CI_ROOT" diff --quiet --no-ext-diff --no-textconv '
        '"$MOLECULE_CI_REF" -- scripts/mcp_pin_lockstep.py '
        "scripts/mcp_built_image_e2e.py"
    )
    attestation_check = 'sha256sum --check "$MCP_ATTESTATION_SHA256"'
    checker = 'python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py"'

    assert fetch.count(git_seal) == 1
    assert build.count(git_seal) == 1
    assert verify.count(git_seal) == 1
    assert build.count(attestation_check) == 1
    assert verify.count(attestation_check) == 1
    assert fetch.index(git_seal) < fetch.index(checker)
    assert fetch.index('mv "$ATTESTATION_TMP" "$ATTESTATION"') < fetch.index(
        'sha256sum "$ATTESTATION" > "$MCP_ATTESTATION_SHA256"'
    )
    assert build.index(git_seal) < build.index(attestation_check)
    assert build.index(attestation_check) < build.index("load_attestation")
    assert verify.index(git_seal) < verify.index("docker cp")
    assert verify.index("docker cp") < verify.index(attestation_check)
    assert verify.index(attestation_check) < verify.index("docker start")


def test_t4_runs_hardened_final_image_mcp_e2e_before_privileged_probe() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    steps = jobs["t4-conformance"]["steps"]
    step_names = [step.get("name") for step in steps]
    verifier_step = _named_step(
        "t4-conformance", "Verify management MCP in the final image"
    )
    script = verifier_step["run"]

    assert step_names.index(
        "Fetch immutable molecule-ci MCP proof tools"
    ) < step_names.index("Build the runtime image")
    assert step_names.index("Build the runtime image") < step_names.index(
        "Verify management MCP in the final image"
    )
    assert step_names.index(
        "Verify management MCP in the final image"
    ) < step_names.index(
        "Run under EXACT tier-4 provisioner flags + assert host-root reach AND token agent-ownership"
    )
    assert verifier_step["if"] == FORK_RUN
    for fragment in (
        "docker create --interactive --name",
        "--network none",
        "--user 1000:1000 --workdir /tmp",
        "--cap-drop ALL --security-opt no-new-privileges",
        "--pids-limit 128 --memory 768m --cpus 1",
        "--tmpfs /tmp:size=64m",
        "--env MOLECULE_PREBAKE_NODE_BIN=/home/agent/.hermes/node/bin",
        '--entrypoint python3 "$T4_TAG"',
        "/mcp_built_image_e2e.py",
        'docker cp "$VERIFIER"',
        'docker start --attach --interactive "$MCP_VERIFY_CONTAINER"',
        '< "$ATTESTATION"',
        "grep -qxF 'mcp-built-image-e2e:sentinel:executed'",
    ):
        assert fragment in script
    assert "--volume" not in script
    assert "--privileged" not in script


def test_t4_build_verifier_and_privileged_probe_share_one_image() -> None:
    steps = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["t4-conformance"]["steps"]
    scripts = [step.get("run", "") for step in steps]
    build_script = _named_step("t4-conformance", "Build the runtime image")["run"]
    verifier_script = _named_step(
        "t4-conformance", "Verify management MCP in the final image"
    )["run"]
    probe_script = _named_step(
        "t4-conformance",
        "Run under EXACT tier-4 provisioner flags + assert host-root reach AND token agent-ownership",
    )["run"]

    assert sum(script.count("docker build") for script in scripts) == 1
    assert T4_TAG_ASSIGNMENT in build_script
    assert T4_TAG_ASSIGNMENT in verifier_script
    assert T4_TAG_ASSIGNMENT in probe_script
    assert 'docker image rm -f "$T4_TAG"' in verifier_script
    cleanup_body = verifier_script[
        verifier_script.index("cleanup_mcp_e2e() {") : verifier_script.index(
            "trap cleanup_mcp_e2e EXIT"
        )
    ]
    assert 'docker rm -f "$MCP_VERIFY_CONTAINER"' in cleanup_body
    assert 'rm -rf -- "$CI_ROOT"' in verifier_script
    assert (
        'rm -f -- "$ATTESTATION" "$MCP_ATTESTATION_SHA256" "$MCP_E2E_LOG"'
        in verifier_script
    )
    assert verifier_script.index("trap cleanup_mcp_e2e EXIT") < verifier_script.index(
        "docker create --interactive --name"
    )
    assert verifier_script.index(
        'docker start --attach --interactive "$MCP_VERIFY_CONTAINER"'
    ) < verifier_script.index('docker rm "$MCP_VERIFY_CONTAINER" >/dev/null')
    assert verifier_script.index("KEEP_T4_IMAGE=1") > verifier_script.index(
        "grep -qxF 'mcp-built-image-e2e:sentinel:executed'"
    )


def test_meta_ci_advisory_matches_the_reviewed_canonical_template() -> None:
    assert hashlib.sha256(META_CI_WORKFLOW.read_bytes()).hexdigest() == META_CI_SHA256


def test_t4_image_cleanup_covers_build_and_probe_failures() -> None:
    steps = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["t4-conformance"]["steps"]
    build_script = next(
        step["run"] for step in steps if "docker build" in step.get("run", "")
    )
    probe_script = next(
        step["run"]
        for step in steps
        if "docker run --rm" in step.get("run", "")
        and "capless-liveness" in step.get("run", "")
    )

    assert build_script.index("trap cleanup_t4_build EXIT") < build_script.index(
        "docker info"
    )
    assert build_script.index("trap cleanup_t4_build EXIT") < build_script.index(
        "load_attestation"
    )
    assert build_script.index("KEEP_T4_IMAGE=1") > build_script.index(
        '"$ACTUAL_RUNTIME_VERSION" != "$EXPECTED_RUNTIME_VERSION"'
    )
    assert probe_script.index("trap '") < probe_script.index("docker run --rm")


def test_checkout_credentials_never_persist() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    checkouts = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts
    assert all(
        step.get("with", {}).get("persist-credentials") is False for step in checkouts
    )


def test_required_conformance_job_runs_runtime_image_contract() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    steps = jobs["conformance"]["steps"]
    scripts = [
        step.get("run", "")
        for step in steps
        if step.get("name") == "Run adapter and private-runtime contracts"
    ]

    assert len(scripts) == 1
    assert "tests/test_ci_runtime_image_pin.py" in scripts[0]


@pytest.mark.parametrize(
    "job_name", ("validate-runtime", "t4-conformance", "shell-tests", "conformance")
)
def test_fork_prs_do_not_execute_repository_code(job_name: str) -> None:
    job = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"][job_name]
    run_steps = [step for step in job["steps"] if "run" in step]

    assert any(step.get("if") == FORK_SKIP for step in run_steps)
    for step in run_steps:
        if step.get("if") == FORK_SKIP:
            continue
        assert FORK_RUN in step.get("if", ""), (
            f"{job_name} step can execute untrusted fork code: {step.get('name', '<unnamed>')}"
        )
