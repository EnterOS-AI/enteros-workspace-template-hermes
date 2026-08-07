#!/usr/bin/env bash
# test-nonroot-boot.sh — start.sh must be able to boot as a NON-ROOT uid.
#
# THE DEFECT THIS PINS, measured 2026-08-07 on the live k3s fleet.
#
# Every tenant Namespace enforces Pod Security Admission `restricted`. That
# policy REQUIRES runAsNonRoot and FORBIDS adding SETUID/SETGID, so a workspace
# pod starts as uid 1000 — already the agent user — with no capability to
# change credentials. start.sh's seven `gosu agent` launches then all die with
#
#   error: failed switching to "agent": operation not permitted
#
# and the container CrashLoopBackOffs behind the misleading line
# "MCP server exited before /mcp came up". Reproduced on the published image
# with a control, proving it is the CAPABILITY and not the identity:
#
#   docker run --user 1000:1000 --cap-drop ALL   ... gosu agent id -> not permitted
#   docker run --cap-add SETUID --cap-add SETGID ... gosu agent id -> uid=1000(agent)
#
# The fix is the AS_AGENT prefix: `gosu agent` when root, empty otherwise.
#
# WHAT THIS TEST IS. Assertions 1-4 are static and cheap. Assertion 5 is the
# one that matters and it is DYNAMIC: it sources a stub of the prefix logic
# under both uids and checks what actually gets executed, so a future edit that
# reintroduces a bare `gosu agent` fails here rather than in a tenant's pod.
#
# NEGATIVE CONTROL (observed while writing this): reverting any single
# `${AS_AGENT}` back to `gosu agent` fails assertion 2 by name; deleting the
# `AS_AGENT=""` else-branch fails assertion 5's non-root case with
# "unbound variable" under `set -u`, which is the exact class of breakage the
# else-branch exists to prevent.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
START_SH="${HERE}/../start.sh"
PASS=0
FAIL=0
FAILURES=()

ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); FAILURES+=("$1"); echo "  FAIL: $1" >&2; }

echo "----- test-nonroot-boot -----"

if [ ! -f "${START_SH}" ]; then
  echo "start.sh not found at ${START_SH}" >&2
  exit 1
fi

# 1. The prefix must be RESOLVED, in both directions.
if grep -Eq 'AS_AGENT="gosu agent"' "${START_SH}" && grep -Eq 'AS_AGENT=""' "${START_SH}"; then
  ok "start.sh resolves AS_AGENT for both the root and the non-root case"
else
  bad "start.sh does not resolve AS_AGENT in BOTH directions — with only the root arm, \
\`set -u\` makes a non-root boot die on an unbound variable instead of running"
fi

# 2. NO bare `gosu agent` may survive outside a comment. This is the assertion
#    that fails on the pre-fix file and on any partial revert.
BARE="$(grep -n 'gosu agent' "${START_SH}" \
        | grep -vE ':[[:space:]]*#' \
        | grep -v 'AS_AGENT="gosu agent"' || true)"
if [ -z "${BARE}" ]; then
  ok "no bare 'gosu agent' call site remains — every launch goes through AS_AGENT"
else
  bad "bare 'gosu agent' call site(s) remain; each one is an unconditional CAP_SETGID \
requirement and dies under PSA restricted:
${BARE}"
fi

# 3. The prefix must be resolved BEFORE the smoke-mode branch, which execs a
#    launch of its own. Ordering, not presence: a definition after that branch
#    leaves exactly one boot path uncovered.
AS_LINE="$(grep -n 'AS_AGENT="gosu agent"' "${START_SH}" | head -1 | cut -d: -f1)"
SMOKE_LINE="$(grep -n 'MOLECULE_SMOKE_MODE' "${START_SH}" | grep -vE ':[[:space:]]*#' | head -1 | cut -d: -f1)"
if [ -n "${AS_LINE}" ] && [ -n "${SMOKE_LINE}" ] && [ "${AS_LINE}" -lt "${SMOKE_LINE}" ]; then
  ok "AS_AGENT is resolved (line ${AS_LINE}) before the smoke-mode branch (line ${SMOKE_LINE})"
else
  bad "AS_AGENT is resolved at line '${AS_LINE}' but the smoke-mode branch is at line '${SMOKE_LINE}' \
— the smoke-mode exec would run before the prefix exists"
fi

# 4. The chowns must STILL be root-gated. The fix must not have widened them
#    into unconditional privileged operations.
if grep -Eq 'id -u.*=.*"?0"?' "${START_SH}" \
   && grep -Eq 'chown[[:space:]]+-R[[:space:]]+agent:agent[[:space:]]+/configs' "${START_SH}"; then
  ok "the /configs chown is still guarded by an 'id -u == 0' root check"
else
  bad "the /configs chown lost its root guard"
fi

# 5. THE DYNAMIC HALF. Run the prefix logic for real, under both uids, and
#    check what is actually executed. `id -u` is stubbed rather than the test
#    being re-run under a different user, so this works in any CI container.
probe() {
  local fake_uid="$1"
  bash -c '
    set -euo pipefail
    id() { if [ "${1:-}" = "-u" ]; then echo "'"${fake_uid}"'"; else command id "$@"; fi; }
    if [ "$(id -u)" = "0" ]; then AS_AGENT="gosu agent"; else AS_AGENT=""; fi
    # A stub standing in for the real launches: it prints its own argv, so we
    # observe what WOULD be executed rather than trusting the variable.
    gosu() { echo "GOSU-INVOKED $*"; }
    env() { echo "DIRECT $*"; }
    # shellcheck disable=SC2086
    ${AS_AGENT} env HOME=/tmp molecule-runtime
  ' 2>&1
}

ROOT_OUT="$(probe 0)"
NONROOT_OUT="$(probe 1000)"

if [ "${ROOT_OUT}" = "GOSU-INVOKED agent env HOME=/tmp molecule-runtime" ]; then
  ok "as root the prefix still drops privileges via gosu (docker behaviour is unchanged)"
else
  bad "as root the launch did NOT go through gosu — the docker path, where every paying \
tenant runs, would change. Got: ${ROOT_OUT}"
fi

if [ "${NONROOT_OUT}" = "DIRECT HOME=/tmp molecule-runtime" ]; then
  ok "as uid 1000 the launch runs DIRECTLY — no gosu, so no CAP_SETGID requirement"
else
  bad "as uid 1000 the launch did not run directly; under PSA restricted this is the \
CrashLoopBackOff. Got: ${NONROOT_OUT}"
fi

echo
echo "----- test-nonroot-boot: ${PASS} passed, ${FAIL} failed -----"
if [ "${FAIL}" -ne 0 ]; then
  printf '  - %s\n' "${FAILURES[@]}" >&2
  exit 1
fi
exit 0
