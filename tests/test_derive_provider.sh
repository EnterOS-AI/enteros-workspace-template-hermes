#!/usr/bin/env bash
# tests/test_derive_provider.sh — sh-style assertion tests for
# scripts/derive-provider.sh. Sourced under bash so PROVIDER is captured
# in the test process.
#
# Run with:   bash tests/test_derive_provider.sh
# Exit code:  0 on success, 1 on any failure.
#
# We deliberately avoid bats / external test frameworks — this template
# repo has no Python/bats test deps installed in CI today; staying in
# pure bash means the script runs anywhere `bash` is on PATH.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DERIVE="${SCRIPT_DIR}/scripts/derive-provider.sh"

if [ ! -f "${DERIVE}" ]; then
  echo "FAIL: cannot find derive-provider.sh at ${DERIVE}" >&2
  exit 1
fi

PASS=0
FAIL=0
FAILURES=()

# derive  <name> <expected_provider>  [VAR=value ...]
# Spawns a clean subshell, sets the supplied env vars, sources the
# script, and asserts $PROVIDER matches.
derive() {
  local name="$1"
  local expected="$2"
  shift 2
  local actual
  actual="$(env -i PATH="$PATH" HOME="$HOME" "$@" bash -c "
    set -uo pipefail
    unset HERMES_INFERENCE_PROVIDER HERMES_INFERENCE_MODEL HERMES_DEFAULT_MODEL
    unset HERMES_API_KEY NOUS_API_KEY OPENROUTER_API_KEY OPENAI_API_KEY
    # Re-export anything the caller passed (env -i wiped them).
    for kv in \"\$@\"; do
      export \"\$kv\"
    done
    PROVIDER=
    . '${DERIVE}'
    printf '%s' \"\$PROVIDER\"
  " _ "$@" 2>&1)"
  if [ "${actual}" = "${expected}" ]; then
    PASS=$((PASS + 1))
    printf "  PASS  %-60s -> %s\n" "${name}" "${actual}"
  else
    FAIL=$((FAIL + 1))
    FAILURES+=("${name}: expected '${expected}', got '${actual}'")
    printf "  FAIL  %-60s expected '%s' got '%s'\n" "${name}" "${expected}" "${actual}"
  fi
}

echo "== derive-provider.sh tests =="

# --- Fix B: env-var rename + legacy fallback -------------------------------
derive "anthropic via HERMES_INFERENCE_MODEL (preferred name)" \
  "anthropic" "HERMES_INFERENCE_MODEL=anthropic/claude-sonnet-4-5"

derive "anthropic via HERMES_DEFAULT_MODEL (legacy fallback)" \
  "anthropic" "HERMES_DEFAULT_MODEL=anthropic/claude-sonnet-4-5"

derive "empty model resolves to auto" \
  "auto" "HERMES_INFERENCE_MODEL="

derive "no env vars at all resolves to auto" \
  "auto"

derive "HERMES_INFERENCE_MODEL preferred over HERMES_DEFAULT_MODEL when both set" \
  "anthropic" \
  "HERMES_INFERENCE_MODEL=anthropic/claude-sonnet-4-5" \
  "HERMES_DEFAULT_MODEL=deepseek/deepseek-v3"

derive "explicit HERMES_INFERENCE_PROVIDER wins over both model vars" \
  "openrouter" \
  "HERMES_INFERENCE_PROVIDER=openrouter" \
  "HERMES_INFERENCE_MODEL=anthropic/claude-sonnet-4-5"

# --- Fix D: 12 newly-added providers --------------------------------------
derive "xai/grok-4 -> xai" \
  "xai" "HERMES_INFERENCE_MODEL=xai/grok-4"

derive "grok/grok-2 -> xai (alias)" \
  "xai" "HERMES_INFERENCE_MODEL=grok/grok-2"

derive "bedrock/anthropic.claude-sonnet-4 -> bedrock" \
  "bedrock" "HERMES_INFERENCE_MODEL=bedrock/anthropic.claude-sonnet-4"

derive "aws/anthropic.claude -> bedrock (alias)" \
  "bedrock" "HERMES_INFERENCE_MODEL=aws/anthropic.claude-sonnet-4"

derive "tencent/hunyuan -> tencent-tokenhub" \
  "tencent-tokenhub" "HERMES_INFERENCE_MODEL=tencent/hunyuan-pro"

derive "tencent-tokenhub/hunyuan -> tencent-tokenhub" \
  "tencent-tokenhub" "HERMES_INFERENCE_MODEL=tencent-tokenhub/hunyuan-pro"

derive "gmi/foo -> gmi" \
  "gmi" "HERMES_INFERENCE_MODEL=gmi/some-model"

derive "qwen-oauth/qwen-max -> qwen-oauth" \
  "qwen-oauth" "HERMES_INFERENCE_MODEL=qwen-oauth/qwen-max"

derive "lmstudio/local-llama -> lmstudio" \
  "lmstudio" "HERMES_INFERENCE_MODEL=lmstudio/local-llama"

derive "lm-studio/local-llama -> lmstudio (alias)" \
  "lmstudio" "HERMES_INFERENCE_MODEL=lm-studio/local-llama"

derive "minimax-oauth/m2 -> minimax-oauth" \
  "minimax-oauth" "HERMES_INFERENCE_MODEL=minimax-oauth/MiniMax-M2"

derive "alibaba-coding-plan/qwen -> alibaba-coding-plan" \
  "alibaba-coding-plan" "HERMES_INFERENCE_MODEL=alibaba-coding-plan/qwen3-coder"

derive "google-gemini-cli/gemini-2.0 -> google-gemini-cli" \
  "google-gemini-cli" "HERMES_INFERENCE_MODEL=google-gemini-cli/gemini-2.0-flash"

derive "openai-codex/codex-mini -> openai-codex" \
  "openai-codex" "HERMES_INFERENCE_MODEL=openai-codex/codex-mini-latest"

derive "copilot/gpt-4o -> copilot" \
  "copilot" "HERMES_INFERENCE_MODEL=copilot/gpt-4o"

derive "copilot-acp/claude-sonnet -> copilot-acp" \
  "copilot-acp" "HERMES_INFERENCE_MODEL=copilot-acp/claude-sonnet-4"

# --- Regression: existing prefixes still work after Fix D additions -------
derive "minimax/* still routes to minimax (not minimax-oauth)" \
  "minimax" "HERMES_INFERENCE_MODEL=minimax/MiniMax-M2.7"

derive "minimax:* routes to minimax for Molecule BYOK ids" \
  "minimax" "HERMES_INFERENCE_MODEL=minimax:MiniMax-M2.7"

derive "alibaba/* still routes to alibaba (not alibaba-coding-plan)" \
  "alibaba" "HERMES_INFERENCE_MODEL=alibaba/qwen3-32b"

derive "qwen/* still routes to alibaba (not qwen-oauth)" \
  "alibaba" "HERMES_INFERENCE_MODEL=qwen/qwen3-32b"

derive "unknown prefix falls through to auto" \
  "auto" "HERMES_INFERENCE_MODEL=some-unknown-vendor/foo-bar"

echo
echo "== summary: ${PASS} passed, ${FAIL} failed =="
if [ "${FAIL}" -gt 0 ]; then
  echo
  echo "failures:"
  for f in "${FAILURES[@]}"; do
    echo "  - ${f}"
  done
  exit 1
fi
exit 0
