"""The runtime image must ship the GitHub CLI (issue #371).

`gh` is a runtime dependency of the seo-agent workspace class, which is built
entirely on it: SETUP.md authenticates with `gh auth refresh` / `gh auth
token`, the agent-policies call `gh api`, and the tenant's scheduled tick
prompt is written around `gh pr list` / `gh pr create` / `gh pr merge`.

It was missing, and it failed SILENTLY — `command not found` is swallowed
inside the agent turn, so the workspace stayed status=online / wedged=false /
error_rate=0 while producing nothing. Confirmed live on workspace 90139d37:
an 11-minute tick left zero git side effects (no branch, no commit, no PR).

The Docker build itself is the strongest check (the Dockerfile runs
`gh --version` as a build-time self-check, so the image cannot ship without
it), but that only runs in the docker jobs. These static assertions run in
the fast unit lane so the packaging regression is caught on every PR.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
INSTALL_SH = ROOT / "install.sh"


def test_dockerfile_installs_github_cli():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "apt-get install -y --no-install-recommends gh" in text, (
        "Dockerfile must install the gh package"
    )
    assert "cli.github.com/packages" in text, (
        "gh must come from the official cli.github.com apt repo"
    )
    assert "githubcli-archive-keyring.gpg" in text, (
        "the apt repo must be signed by the official keyring"
    )


def test_dockerfile_verifies_gh_at_build_time():
    """Fail the BUILD, not the workspace, if gh is ever dropped.

    Without this the next packaging change could silently remove gh and the
    only symptom would be an agent reporting 'queue empty' in production.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "gh --version" in text, (
        "the gh install layer must self-check with `gh --version`"
    )


def test_install_sh_installs_github_cli():
    """The bare-host path must agree with the image.

    A workspace that works in one and not the other is exactly the drift
    that produced #371 — install.sh and the Dockerfile had diverged package
    lists and neither carried gh.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "cli.github.com/packages" in text
    assert "--no-install-recommends gh" in text


def test_keyring_is_readable_by_apt():
    """apt refuses a keyring it cannot read; the chmod is not optional."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg" in text
