#!/usr/bin/env bash
# test-derive-minimax-bridge.sh — contract test for the MiniMax OpenAI-compat
# bridge. Mirrors the style of test-derive-provider.sh: pure env in, env out,
# no network, no container.
#
# REGRESSION THIS PINS: hermes's native `minimax` provider speaks the ANTHROPIC
# shape (POST <base>/v1/messages), which api.minimax.io does not serve —
# measured /v1/messages -> 404 vs /v1/chat/completions -> 401. Without the
# bridge a BYOK MiniMax workspace 404s on every turn and the canvas reports
# "The model provider failed after retries".
set -u

BRIDGE="$(dirname "$0")/derive-minimax-bridge.sh"
fails=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then
    echo "  ok   $1"
  else
    echo "  FAIL $1: expected '$2' got '$3'"
    fails=$((fails + 1))
  fi
}

run() { # $1 = env setup; echoes "PROVIDER|BASE|MODE|MODEL"
  (
    eval "$1"
    . "$BRIDGE" >/dev/null 2>&1
    echo "${PROVIDER:-}|${HERMES_CUSTOM_BASE_URL:-}|${HERMES_CUSTOM_API_MODE:-}|${DEFAULT_MODEL:-}"
  )
}

echo "[test] minimax BYOK bridges to custom + chat_completions + bare model"
check "byok" \
  "custom|https://api.minimax.io/v1|chat_completions|MiniMax-M2.7-highspeed" \
  "$(run 'PROVIDER=minimax; MINIMAX_API_KEY=k; DEFAULT_MODEL=minimax/MiniMax-M2.7-highspeed')"

echo "[test] minimax-cn uses the CN endpoint + CN key"
check "cn" \
  "custom|https://api.minimaxi.com/v1|chat_completions|abab6.5-chat" \
  "$(run 'PROVIDER=minimax-cn; MINIMAX_CN_API_KEY=k; DEFAULT_MODEL=minimax-cn/abab6.5-chat')"

echo "[test] MOLECULE_PLATFORM_LLM_ACTIVE=1 is NOT bridged (platform proxy owns base_url)"
check "platform_active" \
  "minimax|||minimax/M2.7" \
  "$(run 'PROVIDER=minimax; MINIMAX_API_KEY=k; DEFAULT_MODEL=minimax/M2.7; MOLECULE_PLATFORM_LLM_ACTIVE=1')"

echo "[test] legacy platform_managed billing is NOT bridged"
check "platform_billing" \
  "minimax|||minimax/M2.7" \
  "$(run 'PROVIDER=minimax; MINIMAX_API_KEY=k; DEFAULT_MODEL=minimax/M2.7; MOLECULE_LLM_BILLING_MODE=platform_managed')"

echo "[test] operator-supplied HERMES_CUSTOM_* wins"
check "operator" \
  "minimax|http://lmstudio:1234/v1||minimax/M2.7" \
  "$(run 'PROVIDER=minimax; MINIMAX_API_KEY=k; DEFAULT_MODEL=minimax/M2.7; HERMES_CUSTOM_BASE_URL=http://lmstudio:1234/v1; HERMES_CUSTOM_API_KEY=mine')"

echo "[test] no MiniMax key -> no bridge"
check "nokey" \
  "minimax|||minimax/M2.7" \
  "$(run 'PROVIDER=minimax; DEFAULT_MODEL=minimax/M2.7')"

echo "[test] non-minimax provider untouched"
check "other" \
  "anthropic|||anthropic/claude-sonnet-4-5" \
  "$(run 'PROVIDER=anthropic; ANTHROPIC_API_KEY=k; DEFAULT_MODEL=anthropic/claude-sonnet-4-5')"

echo "[test] MINIMAX_BASE_URL override honoured"
check "override" \
  "custom|https://proxy.internal/v1|chat_completions|M2.7" \
  "$(run 'PROVIDER=minimax; MINIMAX_API_KEY=k; MINIMAX_BASE_URL=https://proxy.internal/v1; DEFAULT_MODEL=minimax/M2.7')"

if [ "$fails" -eq 0 ]; then
  echo "PASS"
  exit 0
fi
echo "FAIL ($fails)"
exit 1
