#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HELPER="${ROOT_DIR}/scripts/process-liveness.sh"
START_SH="${ROOT_DIR}/start.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[ -f "${HELPER}" ] || fail "missing ${HELPER}"

# shellcheck source=/dev/null
. "${HELPER}"

tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT

mkdir -p "${tmp}/101"
printf 'State:\tS (sleeping)\n' >"${tmp}/101/status"
process_is_running 101 "${tmp}" || fail "sleeping process was reported dead"

printf 'State:\tZ (zombie)\n' >"${tmp}/101/status"
if process_is_running 101 "${tmp}"; then
  fail "zombie process was reported running"
fi

if process_is_running 999 "${tmp}"; then
  fail "missing process was reported running"
fi
if process_is_running not-a-pid "${tmp}"; then
  fail "invalid pid was reported running"
fi

grep -Fq '. /app/scripts/process-liveness.sh' "${START_SH}" \
  || fail "start.sh does not source the capability-independent liveness helper"
grep -Fq "if ! process_is_running \"\$MCP_PID\"; then" "${START_SH}" \
  || fail "MCP startup still lacks capability-independent liveness detection"
grep -Fq "if ! process_is_running \"\$GATEWAY_PID\"; then" "${START_SH}" \
  || fail "gateway startup still lacks capability-independent liveness detection"
if grep -Fq 'kill -0' "${START_SH}"; then
  fail "start.sh still uses signal-based liveness under a CAP_KILL-free profile"
fi

echo "PASS: process liveness is capability-independent for MCP and gateway children"
