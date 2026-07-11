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
          GOOGLE_API_KEY DEEPSEEK_API_KEY KIMI_API_KEY OPENROUTER_API_KEY \
          MOLECULE_RESOLVED_PROVIDER LLM_PROVIDER
    '"$envspec"'
    '"$BLOCK"'
    printf "%s\n" "${DEFAULT_MODEL}"
  ' | tail -1
}

# run_case_rc ENVSPEC → prints the block's EXIT CODE. Used for the
# platform-managed fail-loud guard, which `exit 1`s before DEFAULT_MODEL is
# printed. Diagnostics go to stderr; both streams are discarded here.
run_case_rc() {
  local envspec="$1"
  # shellcheck disable=SC2086
  env -i PATH="$PATH" bash -c '
    set -u
    unset HERMES_INFERENCE_MODEL HERMES_DEFAULT_MODEL HERMES_INFERENCE_PROVIDER \
          MOLECULE_MODEL MODEL HERMES_API_KEY NOUS_API_KEY ANTHROPIC_API_KEY \
          OPENAI_API_KEY MINIMAX_API_KEY MINIMAX_CN_API_KEY GEMINI_API_KEY \
          GOOGLE_API_KEY DEEPSEEK_API_KEY KIMI_API_KEY OPENROUTER_API_KEY \
          MOLECULE_RESOLVED_PROVIDER LLM_PROVIDER
    '"$envspec"'
    '"$BLOCK"'
    printf "%s\n" "${DEFAULT_MODEL}"
  ' >/dev/null 2>&1
  echo "$?"
}

check_rc() {
  local name="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    PASS=$((PASS + 1)); echo "PASS: $name -> rc=$got"
  else
    FAIL=$((FAIL + 1)); echo "FAIL: $name -> got rc=$got want rc=$want"
  fi
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

# --- Platform-managed fail-loud guard (never silently boot the unpriced BYOK default) ---

# 7. Platform arm (LLM_PROVIDER=platform) with NO injected model → FAIL LOUD
#    (guard exits non-zero) instead of falling to nousresearch/hermes-4-70b.
got=$(run_case_rc 'LLM_PROVIDER=platform')
check_rc "platform arm + no model fails loud (LLM_PROVIDER)" "$got" "1"

# 8. Platform arm (MOLECULE_RESOLVED_PROVIDER=platform, the SSOT) + no model → FAIL LOUD.
got=$(run_case_rc 'MOLECULE_RESOLVED_PROVIDER=platform')
check_rc "platform arm + no model fails loud (MOLECULE_RESOLVED_PROVIDER)" "$got" "1"

# 9. Platform arm WITH the CP-injected model → inherits it, guard does NOT fire.
got=$(run_case 'MOLECULE_RESOLVED_PROVIDER=platform MOLECULE_MODEL=minimax/MiniMax-M2.7')
check "platform arm inherits CP model (no false fire)" "$got" "minimax/MiniMax-M2.7"
got=$(run_case_rc 'MOLECULE_RESOLVED_PROVIDER=platform MOLECULE_MODEL=minimax/MiniMax-M2.7')
check_rc "platform arm + CP model does NOT fail loud" "$got" "0"

# 10. Platform arm with an explicit HERMES_DEFAULT_MODEL → honored, guard does NOT fire.
got=$(run_case 'LLM_PROVIDER=platform HERMES_DEFAULT_MODEL=minimax/MiniMax-M2.7')
check "platform arm honors explicit HERMES_DEFAULT_MODEL" "$got" "minimax/MiniMax-M2.7"

# 11. NON-platform (BYOK) + no CP model + a provider key → key-presence still fires
#     (the guard must NOT hijack ordinary BYOK boots).
got=$(run_case_rc 'MINIMAX_API_KEY=x')
check_rc "byok boot is unaffected by the guard" "$got" "0"

echo "-----"
echo "default-model-selection: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
