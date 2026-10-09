import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_runtime_requirements.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prepare_runtime_requirements", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_runtime_requirement_is_reconstructed_and_filtered(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "# runtime\n"
        "molecules-workspace-runtime>=0.3.10\n"
        "httpx>=0.27.0\n"
    )

    runtime_requirement = _load_module().prepare(
        requirements,
        filtered,
        runtime_version="",
    )

    assert runtime_requirement == "molecules-workspace-runtime>=0.3.10"
    assert filtered.read_text() == "# runtime\nhttpx>=0.27.0\n"


def test_runtime_version_replaces_baseline_specifier_with_exact_pin(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "molecules-workspace-runtime>=0.3.10\n"
        "httpx>=0.27.0\n"
    )

    runtime_requirement = _load_module().prepare(
        requirements,
        filtered,
        runtime_version="0.3.125",
    )

    assert runtime_requirement == "molecules-workspace-runtime==0.3.125"
    assert "molecules-workspace-runtime" not in filtered.read_text()


def test_runtime_direct_reference_is_rejected(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "molecules-workspace-runtime @ https://example.invalid/runtime.whl\n"
    )

    with pytest.raises(ValueError, match="direct, VCS, local, or invalid"):
        _load_module().prepare(requirements, filtered, runtime_version="")


def test_runtime_vcs_egg_cannot_hide_beside_canonical_requirement(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "molecules-workspace-runtime>=0.3.10\n"
        "git+https://example.invalid/runtime.git#egg=molecules-workspace-runtime\n"
    )

    with pytest.raises(ValueError, match="direct, VCS, local, or invalid"):
        _load_module().prepare(requirements, filtered, runtime_version="")


@pytest.mark.parametrize(
    "dependency",
    [
        "httpx @ https://example.invalid/httpx.whl",
        "git+https://example.invalid/httpx.git#egg=httpx",
        "./vendor/httpx.whl",
    ],
)
def test_public_solve_rejects_direct_vcs_and_local_dependencies(
    tmp_path,
    dependency,
):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "molecules-workspace-runtime>=0.3.10\n"
        f"{dependency}\n"
    )

    with pytest.raises(ValueError, match="direct, VCS, local, or invalid"):
        _load_module().prepare(requirements, filtered, runtime_version="")


def test_retired_runtime_distribution_is_rejected(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "molecules-workspace-runtime>=0.3.10\n"
        "molecule-ai-workspace-runtime>=0.1\n"
    )

    with pytest.raises(ValueError, match="retired runtime distribution"):
        _load_module().prepare(requirements, filtered, runtime_version="")


@pytest.mark.parametrize(
    "requirements_text",
    [
        "httpx>=0.27.0\n",
        (
            "molecules-workspace-runtime>=0.3.10\n"
            "molecules-workspace-runtime<1\n"
        ),
    ],
)
def test_exactly_one_runtime_requirement_is_required(
    tmp_path,
    requirements_text,
):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(requirements_text)

    with pytest.raises(ValueError, match="exactly one canonical"):
        _load_module().prepare(requirements, filtered, runtime_version="")


def test_nested_requirements_include_is_rejected(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "molecules-workspace-runtime>=0.3.10\n"
        "-r nested.txt\n"
    )

    with pytest.raises(ValueError, match="pip directives"):
        _load_module().prepare(requirements, filtered, runtime_version="")


def test_backslash_continuation_is_rejected(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(
        "molecules-workspace-runtime>=0.3.10\n"
        "httpx>=0.27.0 \\\n"
    )

    with pytest.raises(ValueError, match="continuations"):
        _load_module().prepare(requirements, filtered, runtime_version="")


@pytest.mark.parametrize(
    "runtime_line",
    [
        "molecules-workspace-runtime[extra]>=0.3.10",
        "molecules-workspace-runtime>=0.3.10; python_version >= '3.11'",
    ],
)
def test_runtime_requirement_rejects_noncanonical_shapes(tmp_path, runtime_line):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text(f"{runtime_line}\n")

    with pytest.raises(ValueError):
        _load_module().prepare(requirements, filtered, runtime_version="")


def test_equivalent_runtime_name_is_reconstructed_canonically(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text("Molecules_Workspace_Runtime>=0.3.10\n")

    runtime_requirement = _load_module().prepare(
        requirements,
        filtered,
        runtime_version="",
    )

    assert runtime_requirement == "molecules-workspace-runtime>=0.3.10"


def test_invalid_runtime_version_does_not_write_filtered_requirements(tmp_path):
    requirements = tmp_path / "requirements.txt"
    filtered = tmp_path / "filtered.txt"
    requirements.write_text("molecules-workspace-runtime>=0.3.10\n")

    with pytest.raises(ValueError, match="invalid RUNTIME_VERSION"):
        _load_module().prepare(
            requirements,
            filtered,
            runtime_version="not a version",
        )

    assert not filtered.exists()
