#!/usr/bin/env python3
"""Separate the private runtime requirement from the public dependency solve."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import InvalidVersion, Version


RUNTIME_PROJECT = "molecules-workspace-runtime"
_RUNTIME_NAME = canonicalize_name(RUNTIME_PROJECT)
_RETIRED_RUNTIME_NAME = canonicalize_name("molecule-ai-workspace-runtime")


def _requirement_text(raw_line: str) -> str:
    return re.split(r"(?<!\S)#", raw_line, maxsplit=1)[0].strip()


def prepare(
    requirements: Path,
    output: Path,
    *,
    runtime_version: str,
) -> str:
    runtime_requirements: list[Requirement] = []
    filtered_lines: list[str] = []

    for raw_line in requirements.read_text().splitlines(keepends=True):
        text = _requirement_text(raw_line)
        if text and raw_line.rstrip().endswith("\\"):
            raise ValueError("requirements backslash continuations are not allowed")
        if text.startswith("-"):
            raise ValueError("requirements pip directives are not allowed")
        if not text:
            filtered_lines.append(raw_line)
            continue

        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            raise ValueError(
                "direct, VCS, local, or invalid requirements are not allowed"
            ) from exc

        if requirement.url:
            raise ValueError(
                "direct, VCS, local, or invalid requirements are not allowed"
            )

        name = canonicalize_name(requirement.name)
        if name == _RETIRED_RUNTIME_NAME:
            raise ValueError("retired runtime distribution is not allowed")
        if name != _RUNTIME_NAME:
            filtered_lines.append(raw_line)
            continue
        if requirement.extras:
            raise ValueError("runtime requirement extras are not allowed")
        if requirement.marker:
            raise ValueError("runtime requirement environment markers are not allowed")
        runtime_requirements.append(requirement)

    if len(runtime_requirements) != 1:
        raise ValueError(
            "requirements.txt must contain exactly one canonical "
            f"{RUNTIME_PROJECT!r} requirement"
        )

    if runtime_version:
        try:
            version = Version(runtime_version)
        except InvalidVersion as exc:
            raise ValueError(f"invalid RUNTIME_VERSION: {runtime_version!r}") from exc
        runtime_requirement = f"{RUNTIME_PROJECT}=={version}"
    else:
        runtime_requirement = f"{RUNTIME_PROJECT}{runtime_requirements[0].specifier}"
    output.write_text("".join(filtered_lines))
    return runtime_requirement


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-version", default="")
    args = parser.parse_args()
    args.output.unlink(missing_ok=True)
    try:
        runtime_requirement = prepare(
            args.requirements,
            args.output,
            runtime_version=args.runtime_version,
        )
    except (OSError, ValueError) as exc:
        args.output.unlink(missing_ok=True)
        parser.error(str(exc))
    print(runtime_requirement)


if __name__ == "__main__":
    main()
