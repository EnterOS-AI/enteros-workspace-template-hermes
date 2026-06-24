from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


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
