from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _logical_dockerfile() -> str:
    return re.sub(r"\\\s*\n\s*", " ", (ROOT / "Dockerfile").read_text())


def test_publish_workflow_exposes_digest_when_promote_reads_it():
    workflow = (ROOT / ".gitea/workflows/publish-image.yml").read_text()

    assert "needs.publish.outputs.digest" in workflow
    assert re.search(r"(?m)^  publish:\n(?:.*\n)*?    outputs:\n(?:.*\n)*?      digest: \$\{\{ steps\.push\.outputs\.digest \}\}", workflow), (
        "publish-image.yml reads needs.publish.outputs.digest, so the publish "
        "job must expose digest from the docker/build-push-action step"
    )


def test_hermes_platform_plugin_ref_is_reproducible():
    dockerfile = (ROOT / "Dockerfile").read_text()

    match = re.search(r"(?m)^ARG HERMES_PLATFORM_MOLECULE_A2A_REF=(\S+)$", dockerfile)
    assert match, "Dockerfile must declare HERMES_PLATFORM_MOLECULE_A2A_REF"
    ref = match.group(1)

    assert ref not in {"main", "master", "latest"}, (
        "HERMES_PLATFORM_MOLECULE_A2A_REF must be a reproducible commit SHA or tag, "
        "not a mutable branch"
    )
    assert re.fullmatch(r"[0-9a-f]{40}", ref) or re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", ref), (
        "HERMES_PLATFORM_MOLECULE_A2A_REF should be a full commit SHA or semver tag"
    )


def test_runtime_wheel_is_acquired_only_from_private_index():
    dockerfile = _logical_dockerfile()
    requirements = (ROOT / "requirements.txt").read_text()

    assert (
        "ARG MOLECULE_RUNTIME_INDEX="
        "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
    ) in dockerfile
    assert dockerfile.count("pip download") == 1
    assert re.search(
        r"pip download --isolated --only-binary=:all: --no-deps\s+"
        r'--index-url "\$MOLECULE_RUNTIME_INDEX"\s+'
        r'--dest /tmp/molecule-runtime "\$runtime_requirement"',
        dockerfile,
    )
    assert "--extra-index-url" not in dockerfile
    assert "--extra-index-url" not in requirements
    assert "-name '*.whl'" in dockerfile
    assert 'test "$wheel_count" -eq 1' in dockerfile
    assert "-name 'molecules_workspace_runtime-*.whl'" in dockerfile
    assert 'test -n "$runtime_wheel"' in dockerfile


def test_runtime_wheel_joins_requirements_in_one_isolated_solve():
    dockerfile = _logical_dockerfile()

    assert re.search(
        r"pip install --isolated --no-cache-dir\s+"
        r'"\$runtime_wheel"\s+-r /tmp/template-requirements\.txt',
        dockerfile,
    )
    assert "prepare-runtime-requirements.py" in dockerfile
    assert '--runtime-version "$RUNTIME_VERSION"' in dockerfile
