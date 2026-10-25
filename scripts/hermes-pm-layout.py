#!/usr/bin/env python3
"""Print where hermes' package manager put the install, asking pm itself.

hermes >= v0.22 installs through its package manager (pm): the pinned
toolchain lives in ~/.hermes/tools/<package>-<version>-<target>/ and the
dependency environment in ~/.hermes/installs/<install key>/environments/
<generation>/. Those names are pm's business, so the image build asks pm for
them instead of globbing for them.

Run it with hermes' own interpreter, as the user who ran the installer, so pm
resolves the same data root the install used:

    ~/.hermes/tools/python-*/bin/python3 -I hermes-pm-layout.py HERMES_ROOT KEY...

Keys, one absolute path printed per key, in order:
    env-python       interpreter of the committed dependency environment
    source-snapshot  the source copy that environment's editable install maps
    uv               pm's uv
    node, npm, npx   pm's Node.js toolchain

Exits non-zero, naming the key, when pm reports nothing or the path is
missing, so a layout change fails the image build instead of shipping.
"""

from __future__ import annotations

import sys
from pathlib import Path


def resolve(root: Path, key: str) -> Path | None:
    from pm import installed_package, uv_launcher
    from pm.environments import committed_venv, venv_python

    if key in ("env-python", "source-snapshot"):
        venv = committed_venv(root)
        if venv is None:
            return None
        return venv_python(venv) if key == "env-python" else venv.parent / "workspace"
    if key == "uv":
        return uv_launcher("uv")
    if key in ("node", "npm"):
        package = installed_package(key)
        return package.binary if package is not None else None
    if key == "npx":
        package = installed_package("npm")
        return package.binary.with_name("npx") if package is not None and package.binary else None
    raise SystemExit(f"hermes-pm-layout: unknown key {key!r}")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(argv[1]).resolve()
    sys.path.insert(0, str(root))
    for key in argv[2:]:
        path = resolve(root, key)
        if path is None or not path.exists():
            print(f"hermes-pm-layout: pm reports no {key} for {root} (got {path})", file=sys.stderr)
            return 1
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
