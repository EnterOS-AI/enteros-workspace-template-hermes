#!/usr/bin/env bash
# test-default-model-selection.sh — offline unit tests for start.sh's
# default-model selection, focused on the CP-unified-model bridge that
# fixes the hermes-concierge 422 ("model has no price catalog entry").
#
# Root cause pinned here: the control-plane provisioner injects the ONE
# resolved model id as MOLECULE_MODEL (SSOT) == MODEL (legacy)
# (molecule-controlplane setUnifiedModel). Hermes historically read ONLY
# HERMES_INFERENCE_MODEL / HERMES_DEFAULT_MODEL and fell through to the
# hardcoded `nousresearch/hermes-4-70b` key-presence default — which has
# NO llm_price_catalog row, so the fail-closed LLM proxy 422s every tool
# turn for a platform-managed concierge (which is Rule-#13-forbidden from
# pinning a model in config.yaml, so it MUST inherit the SSOT default).
#
# start.sh's model-selection logic is inline (not a sourceable helper), so
# this test EXTRACTS the exact block that runs in production — the region
# between the two sentinel comments — and executes it verbatim under
# controlled env. If start.sh's markers change, this test fails loudly
# rather than silently testing nothing.
#
# Covered cases:
#   - concierge path: MOLECULE_MODEL set, no HERMES_* → inherits it (the fix)
#   - MODEL-only (legacy) → inherited when MOLECULE_MODEL absent
#   - MOLECULE_MODEL beats MODEL (SSOT precedence, mirrors CP)
#   - explicit HERMES_INFERENCE_MODEL still wins over CP unified model
#   - explicit HERMES_DEFAULT_MODEL still wins over CP unified model
#   - no CP model + a provider key → key-presence guess still fires (last resort)
#   - concierge default is NOT the unpriced nousresearch/hermes-4-70b

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
START_SH="$HERE/../start.sh"

if [ ! -f "$START_SH" ]; then
  echo "FAIL: start.sh not found at $START_SH"
  exit 2
fi

# Extract the model-selection block: everything from the unified-model
# bridge comment through the end of the key-presence auto-select `fi` +
# the DEFAULT_MODEL assignment. We stop just before the derive-provider
# sourcing (which needs the on-container script).
BLOCK=$(awk '
  /Honor the CP-injected unified workspace model/ { grab=1 }
  grab { print }
  grab && /^DEFAULT_MODEL=/ { exit }
' "$START_SH")

if [ -z "$BLOCK" ]; then
  echo "FAIL: could not extract the model-selection block from start.sh"
  echo "      (the sentinel markers changed — update this test)"
  exit 2
fi

# Sanity: the block must contain BOTH the bridge and the legacy guesses,
# so we know we grabbed the real logic and not a truncated fragment.
case "$BLOCK" in
  *MOLECULE_MODEL*) : ;;
  *) echo "FAIL: extracted block does not reference MOLECULE_MODEL"; exit 2 ;;
esac
case "$BLOCK" in
  *nousresearch/hermes-4-70b*) : ;;
  *) echo "FAIL: extracted block does not contain the key-presence guesses"; exit 2 ;;
esac

PASS=0
FAIL=0

# run_case ENVSPEC → prints the resolved DEFAULT_MODEL. ENVSPEC is a
# space-separated list of KEY=VALUE assignments applied in a fresh subshell.
run_case() {
  local envspec="$1"
  # The extracted block emits informational `echo` lines to stdout; the
  # resolved model is the LAST line (the DEFAULT_MODEL print). Take tail -1
  # so the diagnostics don't pollute the compared value.
  # shellcheck disable=SC2086
  env -i PATH="$PATH" bash -c '
    set -u
    # Clear all the vars the block reads so the host env cannot leak in.
    unset HERMES_INFERENCE_MODEL HERMES_DEFAULT_MODEL HERMES_INFERENCE_PROVIDER \
          MOLECULE_MODEL MODEL HERMES_API_KEY NOUS_API_KEY ANTHROPIC_API_KEY \
          OPENAI_API_KEY MINIMAX_API_KEY MINIMAX_CN_API_KEY GEMINI_API_KEY \
          GOOGLE_API_KEY DEEPSEEK_API_KEY KIMI_API_KEY OPENROUTER_API_KEY
    '"$envspec"'
    '"$BLOCK"'
    printf "%s\n" "${DEFAULT_MODEL}"
  ' | tail -1
}

check() {
  local name="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    PASS=$((PASS + 1)); echo "PASS: $name -> $got"
  else
    FAIL=$((FAIL + 1)); echo "FAIL: $name -> got '$got' want '$want'"
  fi
}

check_not() {
  local name="$1" got="$2" notwant="$3"
  if [ "$got" != "$notwant" ]; then
    PASS=$((PASS + 1)); echo "PASS: $name -> $got (not '$notwant')"
  else
    FAIL=$((FAIL + 1)); echo "FAIL: $name -> got the forbidden value '$notwant'"
  fi
}

# 1. Concierge path — the exact 422 scenario: CP passes MOLECULE_MODEL,
#    no HERMES_* env, no config.yaml pin. Must inherit the CP model.
got=$(run_case 'MOLECULE_MODEL=minimax/MiniMax-M2.7')
check "concierge inherits MOLECULE_MODEL" "$got" "minimax/MiniMax-M2.7"
check_not "concierge is NOT the unpriced default" "$got" "nousresearch/hermes-4-70b"

# 2. Legacy MODEL-only is honored when MOLECULE_MODEL absent.
got=$(run_case 'MODEL=minimax/MiniMax-M2.7')
check "inherits legacy MODEL" "$got" "minimax/MiniMax-M2.7"

# 3. MOLECULE_MODEL beats MODEL (SSOT precedence, mirrors CP).
got=$(run_case 'MOLECULE_MODEL=minimax/MiniMax-M2.7 MODEL=minimax/MiniMax-M2')
check "MOLECULE_MODEL beats MODEL" "$got" "minimax/MiniMax-M2.7"

# 4. Explicit HERMES_INFERENCE_MODEL still wins over the CP unified model.
got=$(run_case 'HERMES_INFERENCE_MODEL=anthropic/claude-sonnet-4-5 MOLECULE_MODEL=minimax/MiniMax-M2.7')
check "HERMES_INFERENCE_MODEL wins" "$got" "anthropic/claude-sonnet-4-5"

# 5. Explicit HERMES_DEFAULT_MODEL still wins over the CP unified model.
got=$(run_case 'HERMES_DEFAULT_MODEL=zai/glm-4.6 MOLECULE_MODEL=minimax/MiniMax-M2.7')
check "HERMES_DEFAULT_MODEL wins" "$got" "zai/glm-4.6"

# 6. No CP unified model at all → key-presence guess still fires (last resort).
got=$(run_case 'MINIMAX_API_KEY=x')
check "key-presence fallback still works (no CP model)" "$got" "minimax/MiniMax-M2.7-highspeed"

echo "-----"
echo "default-model-selection: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
