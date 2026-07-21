"""Contract checks for exact runtime provenance in pull-request image builds."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"
FORK_RUN = "github.event.pull_request.head.repo.fork != true"
FORK_SKIP = "github.event.pull_request.head.repo.fork == true"


def _docker_build_script(job_name: str) -> str:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    scripts = [
        step.get("run", "")
        for step in jobs[job_name]["steps"]
        if "docker build" in step.get("run", "")
    ]
    assert len(scripts) == 1, f"expected one docker build in {job_name}"
    return scripts[0]


@pytest.mark.parametrize("job_name", ("validate-runtime", "t4-conformance"))
def test_pr_image_build_pins_and_verifies_exact_runtime(job_name: str) -> None:
    script = _docker_build_script(job_name)

    assert ".runtime-version" in script
    assert '--build-arg RUNTIME_VERSION="$EXPECTED_RUNTIME_VERSION"' in script
    assert "importlib.metadata import version" in script
    assert 'version("molecules-workspace-runtime")' in script
    assert '"$ACTUAL_RUNTIME_VERSION" != "$EXPECTED_RUNTIME_VERSION"' in script


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
