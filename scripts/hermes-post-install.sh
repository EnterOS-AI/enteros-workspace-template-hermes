#!/usr/bin/env bash
# Finish a hermes install made by vendor/hermes-agent/install.sh. Run as the
# user who ran the installer (HOME must be that install's home), right after
# it. The image build (Dockerfile) and CI's gateway-live-roundtrip job both run
# this, so the gate exercises the install the image ships.
#
#   1. Selects the optional dependency extras (`hermes pm install --extra ...`).
#      The installer selects only `all`. Up to v0.21 every other SDK was
#      pip-installed on first use; the image forbids lazy installs
#      (HERMES_DISABLE_LAZY_INSTALLS=1, see the Dockerfile), so every extra a
#      workspace could reach must be selected here: model providers (anthropic
#      also serves the Anthropic-compatible kimi-coding and zai endpoints),
#      web search backends, image generation, text-to-speech, local
#      speech-to-text, document reading, computer use, the messaging platforms
#      and OTLP export. Not selected: matrix and silk, whose wheels for hermes'
#      Python do not exist (python-olm needs libolm, pilk a C build).
#   2. Links pm's node, npm and npx into ~/.hermes/node/bin (start.sh and
#      /etc/profile.d put that dir on PATH, the management-MCP pre-bake reads
#      it) and into ~/.local/bin, which is on every process's PATH. Before
#      v0.22 the installer unpacked Node into ~/.hermes/node and linked these
#      into ~/.local/bin itself; molecule-runtime finds npx for the management
#      MCP only through PATH.
#   3. Fails unless pm's optional default tools are installed and run:
#      agent-browser + Chromium (browser tool) and cua-driver (computer use,
#      also linked into ~/.local/bin as before). pm leaves them out with only
#      a warning when a download or a system library is missing, and with lazy
#      installs off nothing adds them later.
#   4. Installs the Molecule A2A platform plugin into the committed dependency
#      environment, with pm's uv and --no-deps: its requirements (aiohttp,
#      httpx) are already there at the versions uv.lock chose, and a resolve
#      here could move them. The import and entry-point check proves hermes'
#      plugin discovery can see it. It must come after step 1: selecting
#      extras builds a new environment, which would not contain it.
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
local_bin="$HOME/.local/bin"
layout_py="$(dirname "$0")/hermes-pm-layout.py"

EXTRAS=(
  anthropic bedrock vertex azure-identity mistral
  exa firecrawl parallel-web ddgs
  fal edge-tts tts-premium stt-whisper doc-extract computer-use
  messaging feishu dingtalk teams wecom google-chat
  otlp
)
extra_args=()
for extra in "${EXTRAS[@]}"; do extra_args+=(--extra "$extra"); done
"$local_bin/hermes" pm install "${extra_args[@]}"

shopt -s nullglob
store_pythons=("$HOME"/.hermes/tools/python-*/bin/python3)
shopt -u nullglob
if [ "${#store_pythons[@]}" -ne 1 ] || [ ! -x "${store_pythons[0]}" ]; then
  echo "FATAL: expected exactly one hermes Python under $HOME/.hermes/tools, found: ${store_pythons[*]:-none}" >&2
  exit 1
fi
layout() { "${store_pythons[0]}" -I "$layout_py" "$root" "$@"; }

mkdir -p "$node_bin" "$local_bin"
for tool in node npm npx; do
  target="$(layout "$tool")"  # an assignment, so a failed lookup stops the script
  ln -sfn "$target" "$node_bin/$tool"
  ln -sfn "$target" "$local_bin/$tool"
done
"$node_bin/node" --version
PATH="$node_bin:$PATH" npx --version >/dev/null
echo "node toolchain linked into $node_bin and $local_bin"

for tool in agent-browser chromium cua-driver; do
  binary="$(layout "$tool")"  # an assignment, so a missing tool stops the script
  PATH="$node_bin:$PATH" "$binary" --version >/dev/null
  echo "$tool ready: $binary"
done
cua_driver="$(layout cua-driver)"
ln -sfn "$cua_driver" "$local_bin/cua-driver"

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
