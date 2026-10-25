#!/usr/bin/env bash
# Finish a hermes install made by vendor/hermes-agent/install.sh. Run as the
# user who ran the installer (HOME must be that install's home), right after
# it. The image build (Dockerfile) and CI's gateway-live-roundtrip job both run
# this, so the gate exercises the install the image ships.
#
#   1. Installs the Molecule A2A platform plugin into hermes' committed
#      dependency environment, with pm's uv and --no-deps: its requirements
#      (aiohttp, httpx) are already there at the versions uv.lock chose, and a
#      resolve here could move them. The import and entry-point check proves
#      hermes' plugin discovery can see it.
#   2. Links pm's node, npm and npx into one stable bin dir. Before v0.22 the
#      installer unpacked Node there itself; since v0.22 Node and npm are
#      separate pm packages. start.sh and /etc/profile.d put this dir on PATH
#      (scripts/hermes-node-path.sh) and the management-MCP pre-bake reads it.
#      hermes itself finds Node through pm (HERMES_RUNTIME_DIR), not here.
#
# Every path comes from pm (scripts/hermes-pm-layout.py), never from a guess;
# a layout pm does not report fails here, by name.
#
# Env: HERMES_PLATFORM_MOLECULE_A2A_REF (required, a commit of
#      molecule-ai/hermes-platform-molecule-a2a); HERMES_ROOT (default
#      ~/.hermes/hermes-agent); HERMES_NODE_BIN_DIR (default ~/.hermes/node/bin).
set -euo pipefail

: "${HERMES_PLATFORM_MOLECULE_A2A_REF:?set HERMES_PLATFORM_MOLECULE_A2A_REF}"
root="${HERMES_ROOT:-$HOME/.hermes/hermes-agent}"
node_bin="${HERMES_NODE_BIN_DIR:-$HOME/.hermes/node/bin}"
layout_py="$(dirname "$0")/hermes-pm-layout.py"

shopt -s nullglob
store_pythons=("$HOME"/.hermes/tools/python-*/bin/python3)
shopt -u nullglob
if [ "${#store_pythons[@]}" -ne 1 ] || [ ! -x "${store_pythons[0]}" ]; then
  echo "FATAL: expected exactly one hermes Python under $HOME/.hermes/tools, found: ${store_pythons[*]:-none}" >&2
  exit 1
fi
layout() { "${store_pythons[0]}" -I "$layout_py" "$root" "$@"; }

env_python="$(layout env-python)"
uv="$(layout uv)"
UV_NO_CONFIG=1 "$uv" pip install --python "$env_python" --no-deps --no-cache \
  "hermes-platform-molecule-a2a @ git+https://git.moleculesai.app/molecule-ai/hermes-platform-molecule-a2a.git@${HERMES_PLATFORM_MOLECULE_A2A_REF}"
"$env_python" -I -c '
from importlib.metadata import entry_points
names = {e.name for e in entry_points(group="hermes_agent.plugins")}
assert "molecule_a2a" in names, f"molecule_a2a missing from hermes_agent.plugins: {sorted(names)}"
import hermes_platform_molecule_a2a
print("molecule_a2a plugin importable from", hermes_platform_molecule_a2a.__file__)
'

mkdir -p "$node_bin"
for tool in node npm npx; do
  target="$(layout "$tool")"  # an assignment, so a failed lookup stops the script
  ln -sfn "$target" "$node_bin/$tool"
done
"$node_bin/node" --version
PATH="$node_bin:$PATH" npx --version >/dev/null
echo "node toolchain linked into $node_bin"
