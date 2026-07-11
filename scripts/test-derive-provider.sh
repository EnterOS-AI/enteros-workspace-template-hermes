#!/usr/bin/env bash
# test-derive-provider.sh — offline unit tests for derive-provider.sh.
#
# derive-provider.sh has zero external deps (no network, no hermes install,
# no filesystem writes) — it's pure env-var → PROVIDER string. That makes
# it cheap to exercise in CI as a shell-level test: set env, source, check
# $PROVIDER. Runs in <1s.
#
# Covers regressions:
#   #19 (2026-04-23 E2E) — openai/* + OPENAI_API_KEY must route to `custom`,
#       NOT `openrouter`, even when a global OPENROUTER_API_KEY is present.
#       Previously the wrong-priority rule hijacked operator intent and the
#       A2A reply surfaced OpenRouter's `401 Missing Authentication header`.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/derive-provider.sh"

if [ ! -f "$SCRIPT" ]; then
  echo "FAIL: derive-provider.sh not found at $SCRIPT"
  exit 2
fi

PASS=0
FAIL=0

assert_provider() {
  local label="$1" expected="$2"
  # Run in subshell so env mutations don't leak between cases.
  local actual
  actual=$(bash -c "
    unset HERMES_INFERENCE_PROVIDER
    $3
    PROVIDER=''
    . '$SCRIPT'
    echo \$PROVIDER
  ")
  if [ "$actual" = "$expected" ]; then
    echo "  PASS  $label  →  $actual"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $label  →  got '$actual', expected '$expected'"
    FAIL=$((FAIL+1))
  fi
}

echo "== derive-provider.sh =="

# --- explicit override wins ---
assert_provider "HERMES_INFERENCE_PROVIDER=anthropic beats model slug" "anthropic" '
  HERMES_INFERENCE_PROVIDER=anthropic
  HERMES_DEFAULT_MODEL=openai/gpt-4o
'
assert_provider "resolved BYOK provider rejects stale platform override" "kimi-coding" '
  export MOLECULE_RESOLVED_PROVIDER=kimi-coding
  HERMES_INFERENCE_PROVIDER=platform
  HERMES_DEFAULT_MODEL=kimi-coding/kimi-k2
'

# --- direct-SDK prefixes ---
assert_provider "minimax/M2 → minimax" "minimax" '
  HERMES_DEFAULT_MODEL=minimax/MiniMax-M2
'
assert_provider "minimax:M2 → minimax" "minimax" '
  HERMES_DEFAULT_MODEL=minimax:MiniMax-M2
'
assert_provider "anthropic/claude → anthropic" "anthropic" '
  HERMES_DEFAULT_MODEL=anthropic/claude-sonnet-4-6
'

# --- openai/* priority: REGRESSION TEST for #19 / 2026-04-23 E2E ---
# The scenario: operator provides OPENAI_API_KEY as a workspace secret,
# and the CP/tenant has OPENROUTER_API_KEY set globally. derive-provider
# must pick `custom` (direct OpenAI) to honor operator intent — NOT
# `openrouter` which would hit OR with a key that may be stale/empty.
assert_provider "openai/* + OPENAI_API_KEY + OPENROUTER_API_KEY → custom (#19 regression)" "custom" '
  HERMES_DEFAULT_MODEL=openai/gpt-4o
  export OPENAI_API_KEY=sk-test-openai
  export OPENROUTER_API_KEY=sk-or-test
'

assert_provider "openai/* + only OPENAI_API_KEY → custom" "custom" '
  HERMES_DEFAULT_MODEL=openai/gpt-4o
  export OPENAI_API_KEY=sk-test-openai
'

assert_provider "openai/* + only OPENROUTER_API_KEY → openrouter" "openrouter" '
  HERMES_DEFAULT_MODEL=openai/gpt-4o
  export OPENROUTER_API_KEY=sk-or-test
'

assert_provider "openai/* + no keys → openrouter (fail-loud fallback)" "openrouter" '
  HERMES_DEFAULT_MODEL=openai/gpt-4o
'

# --- nousresearch/* branches ---
assert_provider "nousresearch/* + HERMES_API_KEY → nous" "nous" '
  HERMES_DEFAULT_MODEL=nousresearch/hermes-4-70b
  export HERMES_API_KEY=h-test
'
assert_provider "nousresearch/* + only NOUS_API_KEY → nous" "nous" '
  HERMES_DEFAULT_MODEL=nousresearch/hermes-4-70b
  export NOUS_API_KEY=n-test
'
assert_provider "nousresearch/* + no nous keys → openrouter" "openrouter" '
  HERMES_DEFAULT_MODEL=nousresearch/hermes-4-70b
'

# --- unknown prefix ---
assert_provider "unknown prefix → auto" "auto" '
  HERMES_DEFAULT_MODEL=vendor-x/model-y
'

# --- no model at all ---
assert_provider "no HERMES_DEFAULT_MODEL → auto" "auto" ''

echo
echo "== results: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
