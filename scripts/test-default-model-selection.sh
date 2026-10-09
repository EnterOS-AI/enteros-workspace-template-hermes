#!/usr/bin/env bash
# test-default-model-selection.sh — offline unit tests for start.sh's
# default-model selection + the platform-managed fail-loud guard, both of
# which fix the hermes-concierge 422 ("model has no price catalog entry").
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
# controlled env AND the same `set -euo pipefail` shell options production
# uses. If start.sh's markers change, this test fails loudly rather than
# silently testing nothing.
#
# Covered cases:
#   Inherit (resolves DEFAULT_MODEL):
#     1. concierge path: MOLECULE_MODEL set, no HERMES_* → inherits it (the fix)
#     2. MODEL-only (legacy) → inherited when MOLECULE_MODEL absent
#     3. MOLECULE_MODEL beats MODEL (SSOT precedence, mirrors CP)
#     4. explicit HERMES_INFERENCE_MODEL still wins over CP unified model
#     5. explicit HERMES_DEFAULT_MODEL still wins over CP unified model
#     6. no CP model + a provider key → key-presence guess still fires (last resort)
#     7. concierge default is NOT the unpriced nousresearch/hermes-4-70b
#   Platform-managed fail-loud guard (outcome-based, keyed on the authoritative
#   MOLECULE_RESOLVED_PROVIDER; fires only when the FINAL model is the unpriced
#   BYOK default, so it can never false-fire on a real model):
#     8.  platform arm + no model → resolves nousresearch → guard exits non-zero
#     9.  platform arm + CP MOLECULE_MODEL → inherits it, guard does NOT fire
#     10. platform arm + explicit HERMES_DEFAULT_MODEL → honored, guard does NOT fire
#     11. legacy LLM_PROVIDER=platform ALONE (no SSOT) → NOT armed (no over-fire)
#     12. byok arm (MOLECULE_RESOLVED_PROVIDER!=platform) + provider key → NOT armed
#     13. platform arm + a pinned HERMES_INFERENCE_PROVIDER + CP model → no false-fire

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
START_SH="$HERE/../start.sh"

if [ ! -f "$START_SH" ]; then
  echo "FAIL: start.sh not found at $START_SH"
  exit 2
fi

# Extract the model-selection block: from the unified-model inherit comment
# through the end of the platform-managed guard (a stable sentinel), which
# includes the key-presence auto-select, the DEFAULT_MODEL assignment, and the
# outcome-based fail-loud guard. We stop before the derive-provider sourcing
# (which needs the on-container script).
BLOCK=$(awk '
  /Honor the CP-injected unified workspace model/ { grab=1 }
  grab { print }
  grab && /--- end platform-managed model guard ---/ { exit }
' "$START_SH")

if [ -z "$BLOCK" ]; then
  echo "FAIL: could not extract the model-selection block from start.sh"
  echo "      (the sentinel markers changed — update this test)"
  exit 2
fi

# Sanity: the block must contain the bridge, the legacy guesses, AND the guard,
# so we know we grabbed the real logic and not a truncated fragment.
for needle in 'MOLECULE_MODEL' 'nousresearch/hermes-4-70b' 'MOLECULE_RESOLVED_PROVIDER'; do
  case "$BLOCK" in
    *"$needle"*) : ;;
    *) echo "FAIL: extracted block does not reference '$needle' (markers changed — update this test)"; exit 2 ;;
  esac
done

PASS=0
FAIL=0

# A fresh, host-env-free subshell running the extracted block under the SAME
# shell options production uses (set -euo pipefail), with every var the block
# reads cleared first. $1 is the ENVSPEC (space-separated KEY=VALUE), $2 the
# body appended after the block (e.g. the DEFAULT_MODEL print, or nothing).
_run() {
  local envspec="$1" tail_body="$2"
  # shellcheck disable=SC2086
  env -i PATH="$PATH" bash -c '
    set -euo pipefail
    unset HERMES_INFERENCE_MODEL HERMES_DEFAULT_MODEL HERMES_INFERENCE_PROVIDER \
          MOLECULE_MODEL MODEL HERMES_API_KEY NOUS_API_KEY ANTHROPIC_API_KEY \
          OPENAI_API_KEY MINIMAX_API_KEY MINIMAX_CN_API_KEY GEMINI_API_KEY \
          GOOGLE_API_KEY DEEPSEEK_API_KEY KIMI_API_KEY OPENROUTER_API_KEY \
          MOLECULE_RESOLVED_PROVIDER LLM_PROVIDER
    '"$envspec"'
    '"$BLOCK"'
    '"$tail_body"'
  '
}

# run_case ENVSPEC → the resolved DEFAULT_MODEL (last stdout line; the block's
# own diagnostics go above it).
run_case() { _run "$1" 'printf "%s\n" "${DEFAULT_MODEL}"' | tail -1; }

# run_case_rc ENVSPEC → the block's EXIT CODE (0 = booted; non-zero = guard
# fail-loud). All output discarded.
run_case_rc() { _run "$1" 'printf "%s\n" "${DEFAULT_MODEL}"' >/dev/null 2>&1; echo "$?"; }

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

# --- Platform-managed fail-loud guard (outcome-based) ---

# 8. Platform arm (SSOT) with NO model → model selection lands on the unpriced
#    nousresearch/hermes-4-70b default → guard exits non-zero (fail loud).
got=$(run_case_rc 'MOLECULE_RESOLVED_PROVIDER=platform')
check "platform arm + no model fails loud" "$got" "1"

# 9. Platform arm WITH the CP-injected model → inherits it; guard does NOT fire.
got=$(run_case 'MOLECULE_RESOLVED_PROVIDER=platform MOLECULE_MODEL=minimax/MiniMax-M2.7')
check "platform arm inherits CP model" "$got" "minimax/MiniMax-M2.7"
got=$(run_case_rc 'MOLECULE_RESOLVED_PROVIDER=platform MOLECULE_MODEL=minimax/MiniMax-M2.7')
check "platform arm + CP model does NOT fail loud" "$got" "0"

# 10. Platform arm with an explicit HERMES_DEFAULT_MODEL → honored, no fire.
got=$(run_case 'MOLECULE_RESOLVED_PROVIDER=platform HERMES_DEFAULT_MODEL=minimax/MiniMax-M2.7')
check "platform arm honors explicit HERMES_DEFAULT_MODEL" "$got" "minimax/MiniMax-M2.7"
got=$(run_case_rc 'MOLECULE_RESOLVED_PROVIDER=platform HERMES_DEFAULT_MODEL=minimax/MiniMax-M2.7')
check "platform arm + explicit model does NOT fail loud" "$got" "0"

# 11. Legacy LLM_PROVIDER=platform ALONE (no MOLECULE_RESOLVED_PROVIDER SSOT) must
#     NOT arm the guard — SSOT precedence; a stale legacy pin can't force a crash.
got=$(run_case_rc 'LLM_PROVIDER=platform')
check "legacy LLM_PROVIDER=platform alone does NOT fail loud" "$got" "0"

# 12. BYOK arm (SSOT=minimax, not platform) + a provider key → priced model,
#     guard NOT armed (never hijacks a byok boot).
got=$(run_case_rc 'MOLECULE_RESOLVED_PROVIDER=minimax MINIMAX_API_KEY=x')
check "byok arm is unaffected by the guard (rc)" "$got" "0"
got=$(run_case 'MOLECULE_RESOLVED_PROVIDER=minimax MINIMAX_API_KEY=x')
check "byok arm resolves a priced model" "$got" "minimax/MiniMax-M2.7-highspeed"

# 13. Finding-1 regression: platform arm + a pinned HERMES_INFERENCE_PROVIDER +
#     a delivered CP MOLECULE_MODEL but NO explicit HERMES model. The inherit
#     block is bypassed by the pin so DEFAULT_MODEL resolves empty — but the
#     guard fires ONLY on the nousresearch default, not the empty case, so it
#     does NOT false-fire (refuse) a workspace the CP delivered a model to.
got=$(run_case_rc 'MOLECULE_RESOLVED_PROVIDER=platform HERMES_INFERENCE_PROVIDER=custom MOLECULE_MODEL=minimax/MiniMax-M2.7')
check "platform arm + pinned provider does NOT false-fire on empty" "$got" "0"

echo "-----"
echo "default-model-selection: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
