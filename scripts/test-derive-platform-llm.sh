#!/usr/bin/env bash
# tests/test_derive_platform_llm.sh — sh-style assertion tests for
# scripts/derive-platform-llm.sh (the platform-provider LLM routing override,
# selected by provider==platform — NOT a billing-mode env).
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
    unset MOLECULE_RESOLVED_PROVIDER LLM_PROVIDER HERMES_INFERENCE_PROVIDER MOLECULE_LLM_BASE_URL OPENAI_BASE_URL
    unset MOLECULE_LLM_USAGE_TOKEN ANTHROPIC_API_KEY MOLECULE_PLATFORM_LLM_ACTIVE
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

# (1) Resolved provider is not platform: no-op — PROVIDER, custom vars, and
# model untouched. A bare vendor model + no platform signal is BYOK.
run_case "non-platform provider is a no-op" \
  "0|kimi-coding||||kimi-coding/kimi-k2" \
  "DEFAULT_MODEL=kimi-coding/kimi-k2" "PROVIDER=kimi-coding"

# (2) provider==platform (LLM_PROVIDER=platform) + base + usage token: routes
# through the custom proxy. Note the model has NO platform/ prefix — the
# LLM_PROVIDER signal (core-injected) is what selects platform.
run_case "LLM_PROVIDER=platform routes to custom proxy" \
  "0|custom|${PROXY}|tok-123|chat_completions|moonshot/kimi-k2.6" \
  "LLM_PROVIDER=platform" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (2b) HERMES_INFERENCE_PROVIDER=platform is an equivalent provider==platform
# signal (explicit operator override).
run_case "HERMES_INFERENCE_PROVIDER=platform routes to custom proxy" \
  "0|custom|${PROXY}|tok-123|chat_completions|moonshot/kimi-k2.6" \
  "HERMES_INFERENCE_PROVIDER=platform" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (3) A platform/ model namespace is ITSELF the provider==platform signal (no
# env needed) and the marker is stripped before reaching the proxy.
run_case "platform/ model selects platform + prefix stripped" \
  "0|custom|${PROXY}|tok-123|chat_completions|kimi-k2.6" \
  "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=platform/kimi-k2.6"

# (4) provider==platform but NO base URL: fails closed (rc=1) BEFORE mutating
# PROVIDER / custom vars, so the caller (start.sh) can refuse to boot.
run_case "platform without base url fails closed" \
  "1|kimi-coding||||moonshot/kimi-k2.6" \
  "LLM_PROVIDER=platform" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (4b) provider==platform with a base URL but NO bearer token also fails closed
# (symmetric) — booting with an empty key would defer to a runtime 401.
run_case "platform without bearer fails closed" \
  "1|kimi-coding||||moonshot/kimi-k2.6" \
  "LLM_PROVIDER=platform" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (5) falls back to OPENAI_BASE_URL when MOLECULE_LLM_BASE_URL is absent.
run_case "OPENAI_BASE_URL fallback" \
  "0|custom|${PROXY}|tok-123|chat_completions|moonshot/kimi-k2.6" \
  "LLM_PROVIDER=platform" "OPENAI_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (6) bearer falls back to ANTHROPIC_API_KEY when no usage token.
run_case "ANTHROPIC_API_KEY bearer fallback" \
  "0|custom|${PROXY}|sk-ant-xx|chat_completions|moonshot/kimi-k2.6" \
  "LLM_PROVIDER=platform" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "ANTHROPIC_API_KEY=sk-ant-xx" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# --- SSOT signal: MOLECULE_RESOLVED_PROVIDER (TOP PRECEDENCE) ---------------
# Core's provisioner resolves the provider ONCE and publishes the registry arm
# name here. When set it is authoritative: platform iff value == "platform";
# any other arm is BYOK and must NOT be re-derived from LLM_PROVIDER/the model
# namespace. Only when EMPTY do the legacy signals apply.

# (7) MOLECULE_RESOLVED_PROVIDER=platform is the PRIMARY signal: routes to the
# proxy with no LLM_PROVIDER / no platform/ model marker needed.
run_case "MOLECULE_RESOLVED_PROVIDER=platform routes to proxy" \
  "0|custom|${PROXY}|tok-123|chat_completions|moonshot/kimi-k2.6" \
  "MOLECULE_RESOLVED_PROVIDER=platform" "MOLECULE_LLM_BASE_URL=${PROXY}" \
  "MOLECULE_LLM_USAGE_TOKEN=tok-123" "DEFAULT_MODEL=moonshot/kimi-k2.6"

# (7b) TOP PRECEDENCE: a byok MOLECULE_RESOLVED_PROVIDER wins over the legacy
# signals — LLM_PROVIDER=platform AND a platform/ model marker are BOTH ignored,
# so this is a no-op (PROVIDER + model untouched, marker NOT stripped).
run_case "byok MOLECULE_RESOLVED_PROVIDER overrides legacy platform signals" \
  "0|kimi-coding||||platform/kimi-k2.6" \
  "MOLECULE_RESOLVED_PROVIDER=kimi-coding" "LLM_PROVIDER=platform" \
  "MOLECULE_LLM_BASE_URL=${PROXY}" "MOLECULE_LLM_USAGE_TOKEN=tok-123" \
  "DEFAULT_MODEL=platform/kimi-k2.6" "PROVIDER=kimi-coding"

# (7c) MOLECULE_RESOLVED_PROVIDER=platform fails closed without a base URL,
# same invariant as the legacy-signal path.
run_case "MOLECULE_RESOLVED_PROVIDER=platform fails closed without base url" \
  "1|kimi-coding||||moonshot/kimi-k2.6" \
  "MOLECULE_RESOLVED_PROVIDER=platform" "DEFAULT_MODEL=moonshot/kimi-k2.6"

echo
echo "derive-platform-llm: ${PASS} passed, ${FAIL} failed"
if [ "${FAIL}" -ne 0 ]; then
  printf '%s\n' "${FAILURES[@]}" >&2
  exit 1
fi
echo "test_derive_platform_llm passed"
