#!/usr/bin/env bash
# tests/scripts/test-mcp-configs-dir.sh — regression guard for the
# molecule-mcp server's bearer-token resolution.
#
# Run with:   bash scripts/test-mcp-configs-dir.sh
# Exit code:  0 on success, 1 on any failure.
#
# Pure bash (no bats/pytest) to match the other scripts/test-*.sh in
# this repo — the shell-tests CI job runs them with bare `bash`.
#
# WHAT THIS GUARDS
# ----------------
# The molecule-mcp server (a2a_mcp_server, serves the user-facing
# `list_peers` / `delegate_task` / `send_message_to_user` MCP tools) is
# launched from start.sh as `gosu agent`. Two independent defects made
# every list_peers call 401 with the canned
# "restart the workspace usually re-mints it" message on a brand-new
# Hermes workspace, even though the correct bearer was on disk at
# /configs/.auth_token:
#
#   1. start.sh never `chown -R agent:agent /configs`, so the
#      agent-context MCP server couldn't READ root:0600
#      /configs/.auth_token. (The claude-code template's entrypoint.sh
#      does this chown — fleet contract; configs_dir.py's docstring
#      states /configs is "owned by the agent user".)
#
#   2. The MCP launch used `env HOME=/tmp` (which REPLACES the
#      environment) with no CONFIGS_DIR, so configs_dir.resolve() fell
#      back to $HOME/.molecule-workspace = /tmp/.molecule-workspace
#      (no .auth_token there) -> get_token() None -> no Authorization
#      header -> platform 401.
#
# Both fixes live in start.sh. This test asserts both are present and
# would FAIL against the pre-fix start.sh (verified: the old file has
# neither the `chown -R agent:agent /configs` line nor CONFIGS_DIR on
# the a2a_mcp_server launch).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
START_SH="${SCRIPT_DIR}/start.sh"

PASS=0
FAIL=0
FAILURES=()

ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); echo "FAIL: $1" >&2; }

if [ ! -f "${START_SH}" ]; then
  echo "FAIL: cannot find start.sh at ${START_SH}" >&2
  exit 1
fi

# 1. start.sh must be syntactically valid bash.
if bash -n "${START_SH}" 2>/dev/null; then
  ok "start.sh parses (bash -n)"
else
  bad "start.sh has a bash syntax error (bash -n failed)"
fi

# 2. start.sh must chown /configs to the agent user when running as root.
#    Match is intentionally loose on whitespace/flags but anchored on
#    the agent:agent /configs target so a reordered/reflowed line still
#    counts, while a missing chown (the pre-fix state) fails.
if grep -Eq 'chown[[:space:]]+-R[[:space:]]+agent:agent[[:space:]]+/configs' "${START_SH}"; then
  ok "start.sh chowns /configs to agent (recursive) — agent-context MCP server can read /configs/.auth_token"
else
  bad "start.sh is missing 'chown -R agent:agent /configs' — agent-context molecule-mcp cannot read root:0600 /configs/.auth_token => list_peers 401s"
fi

# 3. The chown must be gated on running as root (so it's a no-op /
#    harmless if start.sh is ever re-execed as the agent user, and so
#    bash -n / shellcheck don't flag an unconditional privileged op).
if grep -Eq 'id -u.*=.*"?0"?' "${START_SH}" \
   && grep -Eq 'chown[[:space:]]+-R[[:space:]]+agent:agent[[:space:]]+/configs' "${START_SH}"; then
  ok "the /configs chown is guarded by an 'id -u == 0' root check"
else
  bad "the /configs chown is not guarded by a root (id -u == 0) check"
fi

# 4. The a2a_mcp_server launch line must carry CONFIGS_DIR=/configs.
#    We extract the launch line(s) and assert CONFIGS_DIR=/configs
#    appears on the same `env ...` invocation. The pre-fix line was:
#       nohup gosu agent env HOME=/tmp \
#           python3 -m molecule_runtime.a2a_mcp_server --transport http --port 9100
#    which this check fails on (no CONFIGS_DIR).
MCP_ENV_LINE="$(grep -nE 'gosu agent env .*HOME=/tmp' "${START_SH}" \
                | grep -B0 -E 'CONFIGS_DIR=/configs' || true)"
# grep above only keeps env lines that ALSO contain CONFIGS_DIR=/configs.
# Additionally require that an a2a_mcp_server launch exists right after
# such a line (within 2 lines) so we're asserting on the MCP server's
# env, not some unrelated gosu invocation.
if awk '
    /gosu agent env .*HOME=\/tmp.*CONFIGS_DIR=\/configs/ { armed=NR }
    armed && NR>armed && NR<=armed+2 && /a2a_mcp_server --transport http --port 9100/ { found=1 }
    END { exit(found?0:1) }
  ' "${START_SH}"; then
  ok "a2a_mcp_server is launched with CONFIGS_DIR=/configs in its (env-replaced) environment"
else
  bad "a2a_mcp_server launch does NOT set CONFIGS_DIR=/configs — configs_dir.resolve() can fall back to /tmp/.molecule-workspace => no bearer => list_peers 401s"
fi

# 5. Belt-and-braces: make sure we didn't accidentally leave a
#    CONFIGS_DIR-less `gosu agent env HOME=/tmp` immediately followed by
#    the a2a_mcp_server launch (the exact pre-fix shape). This is the
#    direct "fails on old code" assertion.
if awk '
    /gosu agent env HOME=\/tmp[[:space:]]*\\$/ && $0 !~ /CONFIGS_DIR/ { armed=NR }
    armed && NR==armed+1 && /a2a_mcp_server --transport http --port 9100/ { bad=1 }
    END { exit(bad?1:0) }
  ' "${START_SH}"; then
  ok "no CONFIGS_DIR-less 'gosu agent env HOME=/tmp' immediately precedes the a2a_mcp_server launch (pre-fix shape is gone)"
else
  bad "found the pre-fix shape: a CONFIGS_DIR-less 'gosu agent env HOME=/tmp \\' line directly before the a2a_mcp_server launch"
fi

echo
echo "----- test-mcp-configs-dir: ${PASS} passed, ${FAIL} failed -----"
if [ "${FAIL}" -ne 0 ]; then
  printf '  - %s\n' "${FAILURES[@]}" >&2
  exit 1
fi
exit 0
