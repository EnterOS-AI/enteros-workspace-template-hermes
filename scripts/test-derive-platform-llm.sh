#!/usr/bin/env bash
# tests/test_derive_platform_llm.sh — sh-style assertion tests for
# scripts/derive-platform-llm.sh (the platform-managed LLM routing override).
#
# Run with:   bash tests/test_derive_platform_llm.sh
# Exit code:  0 on success, 1 on any failure.
#
# Same pure-bash, env -i-isolated approach as test_derive_provider.sh — no
# bats / external deps. Each case spawns a clean subshell, seeds env, sources
# the script, and asserts the emitted routing vars.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${SCRIPT_DIR}/scripts/derive-platform-llm.sh"

if [ ! -f "${TARGET}" ]; then
  echo "FAIL: cannot find derive-platform-llm.sh at ${TARGET}" >&2
  exit 1
fi

PASS=0
FAIL=0
FAILURES=()

# run_case  <name> <expected "rc|PROVIDER|BASE|KEY|MODE|MODEL">  [VAR=value ...]
# Spawns a clean subshell, seeds the supplied env, sources the script with a
# pre-set PROVIDER + DEFAULT_MODEL, and prints rc + the routing vars.
run_case() {
  local name="$1"
  local expected="$2"
  shift 2
  local actual
  actual="$(env -i PATH="$PATH" HOME="$HOME" "$@" bash -c "
    set -uo pipefail
    unset MOLECULE_LLM_BILLING_MODE MOLECULE_LLM_BASE_URL OPENAI_BASE_URL
    unset MOLECULE_LLM_USAGE_TOKEN ANTHROPIC_API_KEY
    for kv in \"\$@\"; do export \"\$kv\"; done
    # Caller seeds DEFAULT_MODEL / PROVIDER via the env list too; default them.
    : \"\${DEFAULT_MODEL:=moonshot/kimi-k2.6}\"
    : \"\${PROVIDER:=kimi-coding}\"
    . '${TARGET}'; rc=\$?
    printf '%s|%s|%s|%s|%s|%s' \"\$rc\" \"\${PROVIDER:-}\" \"\${HERMES_CUSTOM_BASE_URL:-}\" \"\${HERMES_CUSTOM_API_KEY:-}\" \"\${HERMES_CUSTOM_API_MODE:-}\" \"\${DEFAULT_MODEL:-}\"
  " _ "$@" 2>/dev/null)"
  if [ "${actual}" = "${expected}" ]; then
    PASS=$((PASS + 1))
    printf "  PASS  %-46s -> %s\n" "${name}" "${actual}"
  else
    FAIL=$((FAIL + 1))
    FAILURES+=("${name}: expected [${expected}] got [${actual}]")
    printf "  FAIL  %-46s -> %s  (expected %s)\n" "${name}" "${actual}" "${expected}"
  fi
}

PROXY="https://api.moleculesai.app/api/v1/internal/llm/openai/v1"

# (1) Not platform-managed: no-op — PROVIDER, custom vars, and model untouched.
run_case "byok mode is a no-op" \
  "0|kimi-coding||||kimi-coding/kimi-k2" \
  "DEFAULT_MODEL=kimi-coding/kimi-k2" "PROVIDER=kimi-coding"

# (2) platform_managed + base + usage token: routes through custom proxy.
run_case "platform_managed routes to custom proxy" \
  "0|custom|${PROXY}|tok-123|chat_completions|moonshot/kimi-k2.6" \
  "MOLECULE_LLM_BILLING_MODE=platform_managed" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (3) platform/ prefix is stripped before reaching the proxy.
run_case "platform/ prefix stripped" \
  "0|custom|${PROXY}|tok-123|chat_completions|kimi-k2.6" \
  "MOLECULE_LLM_BILLING_MODE=platform_managed" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=platform/kimi-k2.6"

# (4) platform_managed but NO base URL: fails closed (rc=1) BEFORE mutating
# PROVIDER / custom vars, so the caller (start.sh) can refuse to boot.
run_case "platform_managed without base url fails closed" \
  "1|kimi-coding||||moonshot/kimi-k2.6" \
  "MOLECULE_LLM_BILLING_MODE=platform_managed" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (5) falls back to OPENAI_BASE_URL when MOLECULE_LLM_BASE_URL is absent.
run_case "OPENAI_BASE_URL fallback" \
  "0|custom|${PROXY}|tok-123|chat_completions|moonshot/kimi-k2.6" \
  "MOLECULE_LLM_BILLING_MODE=platform_managed" "OPENAI_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (6) bearer falls back to ANTHROPIC_API_KEY when no usage token.
run_case "ANTHROPIC_API_KEY bearer fallback" \
  "0|custom|${PROXY}|sk-ant-xx|chat_completions|moonshot/kimi-k2.6" \
  "MOLECULE_LLM_BILLING_MODE=platform_managed" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "ANTHROPIC_API_KEY=sk-ant-xx" "DEFAULT_MODEL=moonshot/kimi-k2.6"

echo
echo "derive-platform-llm: ${PASS} passed, ${FAIL} failed"
if [ "${FAIL}" -ne 0 ]; then
  printf '%s\n' "${FAILURES[@]}" >&2
  exit 1
fi
echo "test_derive_platform_llm passed"
