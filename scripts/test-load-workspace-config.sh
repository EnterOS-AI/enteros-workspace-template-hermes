#!/usr/bin/env bash
# test-load-workspace-config.sh — offline unit tests for load-workspace-config.sh.
#
# load-workspace-config.sh has one external dep (python3 + pyyaml) but
# both are present on every workspace image and any CI runner with
# Python — making this cheap to exercise as a shell-level test:
# write a fixture YAML, point MOLECULE_CONFIG_PATH at it, source the
# script, check $HERMES_DEFAULT_MODEL / $HERMES_INFERENCE_PROVIDER.
#
# Pins the Option B PR-4 contract that fixed the 2026-04-30 hongmingwang
# tenant 500: hermes start.sh must consume runtime_config.{model,provider}
# from /configs/config.yaml so canvas Config-tab picks reach the gateway.
#
# Covered cases:
#   - YAML missing → no-op (silent skip)
#   - Malformed YAML → no-op (silent skip, no exit)
#   - runtime_config absent → no-op
#   - runtime_config.{model,provider} present → exported
#   - Pre-set env vars win over YAML values (operator override)
#   - Only model present (provider stays unset)
#   - Only provider present (model stays unset)
#   - Non-string scalar values (int) coerce to string

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/load-workspace-config.sh"

if [ ! -f "$SCRIPT" ]; then
  echo "FAIL: load-workspace-config.sh not found at $SCRIPT"
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP: python3 not on PATH — load-workspace-config.sh requires it"
  exit 0
fi
if ! python3 -c "import yaml" 2>/dev/null; then
  echo "SKIP: pyyaml not importable — install with: pip install pyyaml"
  exit 0
fi

PASS=0
FAIL=0
TMPDIR=$(mktemp -d -t lwc-test.XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT

# Run the script in a fresh subshell with given env + a fixture
# config.yaml, then echo the resulting env-var values for comparison.
# Subshell isolation prevents env mutations from leaking between cases.
run_case() {
  local label="$1" yaml_content="$2" pre_env="$3" expected_model="$4" expected_provider="$5"

  local case_dir="$TMPDIR/$$.$RANDOM"
  mkdir -p "$case_dir"
  if [ "$yaml_content" != "<missing>" ]; then
    printf '%s' "$yaml_content" > "$case_dir/config.yaml"
  fi

  local actual
  actual=$(bash -c "
    set +e
    unset HERMES_DEFAULT_MODEL HERMES_INFERENCE_PROVIDER
    $pre_env
    export MOLECULE_CONFIG_PATH='$case_dir'
    . '$SCRIPT' 2>/dev/null
    echo \"MODEL=\${HERMES_DEFAULT_MODEL:-}\"
    echo \"PROVIDER=\${HERMES_INFERENCE_PROVIDER:-}\"
  ")

  local actual_model actual_provider
  actual_model=$(echo "$actual" | grep -E '^MODEL=' | sed 's/^MODEL=//')
  actual_provider=$(echo "$actual" | grep -E '^PROVIDER=' | sed 's/^PROVIDER=//')

  if [ "$actual_model" = "$expected_model" ] && [ "$actual_provider" = "$expected_provider" ]; then
    echo "  PASS  $label  (model='$actual_model' provider='$actual_provider')"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $label"
    echo "        got      model='$actual_model' provider='$actual_provider'"
    echo "        expected model='$expected_model' provider='$expected_provider'"
    FAIL=$((FAIL+1))
  fi

  rm -rf "$case_dir"
}

echo "== load-workspace-config.sh =="

# --- File missing → no-op ---
run_case "missing config.yaml" "<missing>" "" "" ""

# --- Malformed YAML → no-op (silent skip) ---
run_case "malformed YAML" "providers: [not valid: {{{" "" "" ""

# --- runtime_config absent → no-op ---
run_case "no runtime_config block" "name: foo
runtime: hermes
" "" "" ""

# --- runtime_config not a dict → no-op ---
run_case "runtime_config is a string" "runtime_config: \"not-a-dict\"
" "" "" ""

# --- Both fields present → both exported ---
run_case "both fields" "runtime_config:
  model: nousresearch/hermes-4-70b
  provider: openrouter
" "" "nousresearch/hermes-4-70b" "openrouter"

# --- Operator env var beats YAML for both ---
run_case "env override beats both" "runtime_config:
  model: nousresearch/hermes-4-70b
  provider: openrouter
" "export HERMES_DEFAULT_MODEL=anthropic/claude-sonnet-4-6
export HERMES_INFERENCE_PROVIDER=anthropic" "anthropic/claude-sonnet-4-6" "anthropic"

# --- Only model present (provider stays unset) ---
run_case "model only" "runtime_config:
  model: deepseek/deepseek-v4-pro
" "" "deepseek/deepseek-v4-pro" ""

# --- Only provider present (model stays unset) ---
run_case "provider only" "runtime_config:
  provider: zai
" "" "" "zai"

# --- Empty string values are skipped ---
run_case "empty-string fields skipped" "runtime_config:
  model: \"\"
  provider: \"\"
" "" "" ""

# --- Non-string scalar (int) coerces to string ---
# Not a real-world case but pins the str() coercion in the python helper
# so a future YAML schema with numeric fields doesn't crash the boot.
run_case "non-string scalar coerces" "runtime_config:
  model: 42
  provider: 7
" "" "42" "7"

# --- Mixed: env var set for one, YAML provides the other ---
run_case "partial env override" "runtime_config:
  model: minimax/MiniMax-M2.7-highspeed
  provider: minimax
" "export HERMES_INFERENCE_PROVIDER=anthropic" "minimax/MiniMax-M2.7-highspeed" "anthropic"

echo
echo "Total: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
