#!/usr/bin/env bash
# test-install-prefix-strip.sh — regression tests for the step-(B) openai/
# prefix strip in install.sh.
#
# install.sh's prefix strip used to be coupled to step (A)'s auto-fill
# guard: if the operator pre-configured HERMES_CUSTOM_{BASE_URL,API_KEY,
# API_MODE}, step (A) skipped, which also skipped the prefix strip.
# That broke molecule-core#1987 (staging E2E) which pins HERMES_CUSTOM_*
# to bypass derive-provider.sh flakiness.
#
# This test pins the decoupled behavior: strip when the final URL is
# api.openai.com, keep the prefix otherwise.
#
# Design: rather than partial-source install.sh (which boots hermes,
# installs apt packages, etc.), we inline the exact two blocks here and
# a `verify-parity` step greps install.sh to ensure the inlined logic
# matches what ships.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$HERE/../install.sh"

PASS=0
FAIL=0

# The logic under test — mirrored from install.sh. If install.sh changes,
# either keep this in sync or make the test fail the parity check below.
apply_install_logic() {
  # (A) auto-fill defaults when operator hasn't configured custom
  if [ "${PROVIDER:-}" = "custom" ] \
      && [ -n "${OPENAI_API_KEY:-}" ] \
      && [ -z "${HERMES_CUSTOM_BASE_URL:-}" ] \
      && [ -z "${HERMES_CUSTOM_API_KEY:-}" ]; then
    export HERMES_CUSTOM_BASE_URL="https://api.openai.com/v1"
    export HERMES_CUSTOM_API_KEY="${OPENAI_API_KEY}"
    export HERMES_CUSTOM_API_MODE="chat_completions"
  fi

  # (B) strip openai/ prefix iff final URL is api.openai.com (decoupled from A)
  if [[ "${HERMES_CUSTOM_BASE_URL:-}" =~ ^https?://api\.openai\.com(/|$) ]]; then
    DEFAULT_MODEL="${DEFAULT_MODEL#openai/}"
  fi
}

assert_model() {
  local label="$1" expected="$2"
  shift 2
  local actual
  actual=$(bash -c "
    set -u
    $(declare -f apply_install_logic)
    PROVIDER=''
    OPENAI_API_KEY=''
    OPENROUTER_API_KEY=''
    MINIMAX_API_KEY=''
    HERMES_CUSTOM_BASE_URL=''
    HERMES_CUSTOM_API_KEY=''
    HERMES_CUSTOM_API_MODE=''
    DEFAULT_MODEL=''
    $*
    apply_install_logic
    printf '%s' \"\$DEFAULT_MODEL\"
  " 2>/dev/null)
  if [ "$actual" = "$expected" ]; then
    echo "  PASS  $label  →  $actual"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $label  →  got '$actual', expected '$expected'"
    FAIL=$((FAIL+1))
  fi
}

echo "== install.sh prefix-strip =="

# --- Case A: default bridge path (no operator HERMES_CUSTOM_*) ---
assert_model "A: default bridge strips openai/" "gpt-4o" '
  PROVIDER=custom
  OPENAI_API_KEY=sk-test
  DEFAULT_MODEL=openai/gpt-4o
'

# --- Case B: operator-configured HERMES_CUSTOM_* → OpenAI (the #1987 path) ---
assert_model "B: operator-pinned OpenAI URL strips openai/ (#1987)" "gpt-4o" '
  PROVIDER=custom
  OPENAI_API_KEY=sk-test
  HERMES_CUSTOM_BASE_URL=https://api.openai.com/v1
  HERMES_CUSTOM_API_KEY=sk-test
  HERMES_CUSTOM_API_MODE=chat_completions
  DEFAULT_MODEL=openai/gpt-4o
'

# --- Case C: operator-configured HERMES_CUSTOM_* → vLLM/local server → NO strip ---
assert_model "C: vLLM URL keeps prefix (user namespace)" "openai/my-finetune" '
  PROVIDER=custom
  OPENAI_API_KEY=sk-test
  HERMES_CUSTOM_BASE_URL=http://localhost:8000/v1
  HERMES_CUSTOM_API_KEY=none
  DEFAULT_MODEL=openai/my-finetune
'

# --- Case D: PROVIDER=openrouter → no strip (OR expects prefix) ---
assert_model "D: openrouter keeps prefix" "openai/gpt-4o" '
  PROVIDER=openrouter
  OPENROUTER_API_KEY=sk-or-test
  DEFAULT_MODEL=openai/gpt-4o
'

# --- Case E: PROVIDER=minimax, model has different prefix → no strip ---
assert_model "E: minimax model untouched" "minimax/MiniMax-M2.7" '
  PROVIDER=minimax
  MINIMAX_API_KEY=test
  DEFAULT_MODEL=minimax/MiniMax-M2.7
'

# --- Case F: OpenAI URL but model already bare (idempotent) ---
assert_model "F: idempotent on already-bare model" "gpt-4o" '
  PROVIDER=custom
  OPENAI_API_KEY=sk-test
  HERMES_CUSTOM_BASE_URL=https://api.openai.com/v1
  HERMES_CUSTOM_API_KEY=sk-test
  DEFAULT_MODEL=gpt-4o
'

# --- Case G: lookalike domain must NOT match ---
assert_model "G: lookalike domain api.openai.com.evil.internal NOT stripped" "openai/gpt-4o" '
  PROVIDER=custom
  HERMES_CUSTOM_BASE_URL=https://api.openai.com.evil.internal/v1
  HERMES_CUSTOM_API_KEY=stolen
  DEFAULT_MODEL=openai/gpt-4o
'

# --- Case H: http:// (not https) also matches (for local proxy fronting OpenAI) ---
assert_model "H: http:// api.openai.com still strips" "gpt-4o" '
  PROVIDER=custom
  HERMES_CUSTOM_BASE_URL=http://api.openai.com/v1
  HERMES_CUSTOM_API_KEY=sk-test
  DEFAULT_MODEL=openai/gpt-4o
'

# --- Case I: subdomain of api.openai.com (unlikely) must NOT match ---
assert_model "I: beta.api.openai.com NOT stripped" "openai/gpt-4o" '
  PROVIDER=custom
  HERMES_CUSTOM_BASE_URL=https://beta.api.openai.com/v1
  HERMES_CUSTOM_API_KEY=sk-test
  DEFAULT_MODEL=openai/gpt-4o
'

# --- Parity check: install.sh must contain the exact logic we inlined here ---
# Uses grep -F (fixed string) to avoid regex escaping hell. Each pattern is
# a short unique substring from the real install.sh block.
echo
echo "== parity with install.sh =="
PARITY_FAIL=0
for pattern in \
  '[ "${PROVIDER}" = "custom" ] && [ -n "${OPENAI_API_KEY:-}" ] && [ -z "${HERMES_CUSTOM_BASE_URL:-}" ] && [ -z "${HERMES_CUSTOM_API_KEY:-}" ]' \
  '=~ ^https?://api\.openai\.com(/|$)' \
  'DEFAULT_MODEL="${DEFAULT_MODEL#openai/}"'; do
  if ! grep -F -q -- "$pattern" "$INSTALL"; then
    echo "  FAIL  install.sh missing substring: $pattern"
    PARITY_FAIL=$((PARITY_FAIL+1))
  fi
done
if [ "$PARITY_FAIL" -eq 0 ]; then
  echo "  PASS  install.sh contains expected logic blocks"
  PASS=$((PASS+1))
else
  FAIL=$((FAIL+PARITY_FAIL))
fi

echo
echo "== results: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
