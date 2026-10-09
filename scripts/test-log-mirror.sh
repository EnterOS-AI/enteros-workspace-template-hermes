#!/usr/bin/env bash
set -euo pipefail

# Unit test for scripts/log-mirror.sh — the file-only-log -> container-stdout
# mirror that makes the hermes gateway + Molecule MCP server logs visible in
# `docker logs` / Dozzle. Also asserts start.sh actually wires the mirror in
# for BOTH log files.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HELPER="${ROOT_DIR}/scripts/log-mirror.sh"
START_SH="${ROOT_DIR}/start.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[ -f "${HELPER}" ] || fail "missing ${HELPER}"

# shellcheck source=/dev/null
. "${HELPER}"

tmp=$(mktemp -d)
# Kill any followers this test spawned (their argv contains $tmp) and clean up.
trap 'pkill -f "tail -n0 -F ${tmp}" 2>/dev/null || true; rm -rf "${tmp}"' EXIT

# --- 1. New lines appended to the log reach the mirror target (container stdout)
log="${tmp}/gateway.log"
mirror="${tmp}/stdout.mirror"
: >"${log}"
: >"${mirror}"

MIRROR_STDOUT_TARGET="${mirror}" mirror_log_to_stdout "${log}" >/dev/null \
  || fail "mirror_log_to_stdout returned non-zero for a writable target"

# `tail -n0 -F` starts at the file's current end, so only lines written AFTER it
# attaches are mirrored (in production the gateway logs long after the follower
# is up, so nothing is lost). Emit on every poll iteration so this test never
# races the follower's sub-second attach window.
found=0
for _ in $(seq 1 50); do
  printf 'boot line to dozzle\n' >>"${log}"
  if grep -q 'boot line to dozzle' "${mirror}" 2>/dev/null; then
    found=1
    break
  fi
  sleep 0.2
done
[ "${found}" -eq 1 ] || fail "appended log line was not mirrored to the stdout target"

# --- 2. Idempotent: a second call does NOT stack a duplicate follower
MIRROR_STDOUT_TARGET="${mirror}" mirror_log_to_stdout "${log}" >/dev/null \
  || fail "second mirror_log_to_stdout call returned non-zero"

if command -v pgrep >/dev/null 2>&1; then
  n=$(pgrep -f "tail -n0 -F ${log}\$" | wc -l | tr -d ' ')
  [ "${n}" -eq 1 ] || fail "expected exactly 1 follower for ${log}, found ${n} (not idempotent)"
fi

# --- 3. Un-openable target: skip cleanly, never abort boot, spawn no follower
log2="${tmp}/mcp.log"
: >"${log2}"
set +e
MIRROR_STDOUT_TARGET="${tmp}/does-not-exist/stdout.mirror" \
  mirror_log_to_stdout "${log2}" >/dev/null 2>&1
rc=$?
set -e
[ "${rc}" -eq 0 ] || fail "mirror_log_to_stdout must return 0 on an un-openable target (rc=${rc})"
if command -v pgrep >/dev/null 2>&1; then
  if pgrep -f "tail -n0 -F ${log2}\$" >/dev/null 2>&1; then
    fail "a follower was spawned even though the stdout target was un-openable"
  fi
fi

# --- 4. Missing arg is a hard error (guards against a silent no-op)
set +e
( mirror_log_to_stdout >/dev/null 2>&1 )
rc=$?
set -e
[ "${rc}" -ne 0 ] || fail "mirror_log_to_stdout with no log path should error"

# --- 5. start.sh actually sources the helper and mirrors BOTH file-only logs
grep -Fq '. /app/scripts/log-mirror.sh' "${START_SH}" \
  || fail "start.sh does not source the log-mirror helper"
grep -Fq 'mirror_log_to_stdout "$LOG_FILE"' "${START_SH}" \
  || fail "start.sh does not mirror the gateway log (\$LOG_FILE) to stdout"
grep -Fq 'mirror_log_to_stdout "$MCP_LOG"' "${START_SH}" \
  || fail "start.sh does not mirror the MCP server log (\$MCP_LOG) to stdout"

echo "PASS: file-only gateway + MCP logs are mirrored to container stdout (Dozzle), idempotently and fail-safe"
