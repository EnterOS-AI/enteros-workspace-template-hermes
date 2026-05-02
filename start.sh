#!/usr/bin/env bash
# Boot both processes inside the workspace container:
#   1. The real hermes-agent gateway with the OpenAI-compat API server
#      platform enabled, listening on 127.0.0.1:8642.
#   2. molecule-runtime (our A2A server + bridge adapter) on :8000.
#
# The two talk over loopback. The platform only exposes :8000 — the
# hermes-agent API is an internal implementation detail and never
# reachable from outside the container.

set -euo pipefail

# Boot-smoke contract (molecule-core#2275): the publish-image gate
# invokes the runtime with stub creds and no network so it can
# exercise lazy imports inside executor.execute(). The hermes
# gateway needs valid creds + a writable log file, neither of which
# exist in the smoke env. Skip directly to molecule-runtime — the
# runtime's smoke_mode short-circuit fires after create_executor()
# returns and exits before any A2A traffic is attempted. Real
# production boots are unaffected.
if [ "${MOLECULE_SMOKE_MODE:-0}" = "1" ]; then
  echo "[start.sh] MOLECULE_SMOKE_MODE=1 — skipping hermes gateway spawn"
  exec molecule-runtime
fi

HERMES_HOME="/tmp/.hermes"
ENV_FILE="${HERMES_HOME}/.env"
HERMES_CONFIG="${HERMES_HOME}/config.yaml"
LOG_FILE="/tmp/hermes-gateway.log"

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"
chown agent:agent "$LOG_FILE"

# --- Generate a per-container API_SERVER_KEY ---
# hermes-agent requires a bearer token on the api-server platform. We
# generate a random value per boot and inject it into both processes via
# env — molecule_runtime's executor reads the same var at request time.
if [ -z "${API_SERVER_KEY:-}" ]; then
  API_SERVER_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)"
  export API_SERVER_KEY
fi

install -d -o agent -g agent "$HERMES_HOME"

# --- Write hermes-agent's .env ---
# API_SERVER_ENABLED must be true and the bearer must match. Every
# provider key hermes-agent knows about is forwarded from the container
# env IF it's set — see docs/CONFIGURATION.md#provider-matrix for the
# authoritative list. Adding a new key here also needs a matching
# required_env entry in config.yaml.
cat >"$ENV_FILE" <<EOF
API_SERVER_ENABLED=true
API_SERVER_KEY=${API_SERVER_KEY}
API_SERVER_HOST=${API_SERVER_HOST:-127.0.0.1}
API_SERVER_PORT=${API_SERVER_PORT:-8642}
# Provider-selection override (optional; empty = hermes auto-detect).
${HERMES_INFERENCE_PROVIDER:+HERMES_INFERENCE_PROVIDER=${HERMES_INFERENCE_PROVIDER}}
# Auxiliary model defaults — used by vision, web summarization, MoA.
${HERMES_AUXILIARY_PROVIDER:+HERMES_AUXILIARY_PROVIDER=${HERMES_AUXILIARY_PROVIDER}}
# ── Primary inference providers (keyed) ───────────────────────
${HERMES_API_KEY:+HERMES_API_KEY=${HERMES_API_KEY}}
${NOUS_API_KEY:+NOUS_API_KEY=${NOUS_API_KEY}}
${OPENROUTER_API_KEY:+OPENROUTER_API_KEY=${OPENROUTER_API_KEY}}
${OPENAI_API_KEY:+OPENAI_API_KEY=${OPENAI_API_KEY}}
${ANTHROPIC_API_KEY:+ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}}
${GEMINI_API_KEY:+GEMINI_API_KEY=${GEMINI_API_KEY}}
${GOOGLE_API_KEY:+GOOGLE_API_KEY=${GOOGLE_API_KEY}}
${DEEPSEEK_API_KEY:+DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}}
${GLM_API_KEY:+GLM_API_KEY=${GLM_API_KEY}}
${KIMI_API_KEY:+KIMI_API_KEY=${KIMI_API_KEY}}
${KIMI_CN_API_KEY:+KIMI_CN_API_KEY=${KIMI_CN_API_KEY}}
${MINIMAX_API_KEY:+MINIMAX_API_KEY=${MINIMAX_API_KEY}}
${MINIMAX_CN_API_KEY:+MINIMAX_CN_API_KEY=${MINIMAX_CN_API_KEY}}
${DASHSCOPE_API_KEY:+DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY}}
${XIAOMI_API_KEY:+XIAOMI_API_KEY=${XIAOMI_API_KEY}}
${ARCEEAI_API_KEY:+ARCEEAI_API_KEY=${ARCEEAI_API_KEY}}
${NVIDIA_API_KEY:+NVIDIA_API_KEY=${NVIDIA_API_KEY}}
${OLLAMA_API_KEY:+OLLAMA_API_KEY=${OLLAMA_API_KEY}}
${HF_TOKEN:+HF_TOKEN=${HF_TOKEN}}
${AI_GATEWAY_API_KEY:+AI_GATEWAY_API_KEY=${AI_GATEWAY_API_KEY}}
${KILOCODE_API_KEY:+KILOCODE_API_KEY=${KILOCODE_API_KEY}}
${OPENCODE_ZEN_API_KEY:+OPENCODE_ZEN_API_KEY=${OPENCODE_ZEN_API_KEY}}
${OPENCODE_GO_API_KEY:+OPENCODE_GO_API_KEY=${OPENCODE_GO_API_KEY}}
# GitHub Copilot (OAuth or token)
${COPILOT_GITHUB_TOKEN:+COPILOT_GITHUB_TOKEN=${COPILOT_GITHUB_TOKEN}}
${GH_TOKEN:+GH_TOKEN=${GH_TOKEN}}
EOF
chown agent:agent "$ENV_FILE"
chmod 600 "$ENV_FILE"

# --- Seed a minimal ~/.hermes/config.yaml if not already present ---
# The container image runs install.sh with --skip-setup so no config
# is generated at build time. Without an explicit provider, hermes
# errors at request time with "No LLM provider configured" even when
# a provider key is present in .env — the config.yaml is the primary
# source of truth, .env only holds keys.
#
# Writing an explicit provider here also avoids the auto-detect
# falling through to openai-codex (OAuth-only) when OPENAI_API_KEY is
# set but OPENROUTER_API_KEY isn't — source of the 401 "Missing
# Authentication header" in early testing.
# Unconditionally overwrite — the hermes installer drops its
# `cli-config.yaml.example` in place as `~/.hermes/config.yaml`
# (defaulting to anthropic/claude-opus-4.6 + provider:auto) which
# doesn't match the workspace's intended model. Our template owns
# the selection; operators override via HERMES_INFERENCE_PROVIDER
# + HERMES_DEFAULT_MODEL env, or by editing config.yaml at runtime
# inside the container.
# Pull HERMES_DEFAULT_MODEL + HERMES_INFERENCE_PROVIDER out of
# /configs/config.yaml (canvas Config tab values, written by CP
# user-data per task #197). Env-var overrides still win — the helper
# only sets vars that aren't already set. Sourced for env mutation.
# Dockerfile COPYs scripts/ to /app/scripts; fall back to /scripts
# for dev environments running start.sh with a different WORKDIR.
#
# Runs BEFORE the API-key-based auto-selection block below so a
# canvas-set provider/model wins over a key-presence guess. Operators
# who explicitly picked GLM-4.6 in the UI shouldn't get bumped to
# anthropic/* just because ANTHROPIC_API_KEY happens to be in env too.
LOAD_CONFIG_SCRIPT="/app/scripts/load-workspace-config.sh"
[ -f "$LOAD_CONFIG_SCRIPT" ] || LOAD_CONFIG_SCRIPT="/scripts/load-workspace-config.sh"
[ -f "$LOAD_CONFIG_SCRIPT" ] && . "$LOAD_CONFIG_SCRIPT"

# Pick a default model. The fallback used to be `nousresearch/hermes-4-70b`
# unconditionally, which derives PROVIDER=openrouter when no Nous key is
# present — and if OPENROUTER_API_KEY isn't set either, hermes-agent boots
# with a config that points at a provider with no usable key, then 500s
# at request time with "No LLM provider configured". Surfaces as a real
# user-facing error whenever a workspace is provisioned with a single
# provider key (e.g. just MINIMAX_API_KEY) but no explicit model
# selection — the canvas's "set key, save, send" flow.
#
# Fix: when HERMES_DEFAULT_MODEL is unset and HERMES_INFERENCE_PROVIDER
# is unset, pick the default model based on which API key is actually
# present in env. Keeps the behaviour-when-everything-is-set unchanged
# (operator-supplied HERMES_DEFAULT_MODEL still wins, including the
# config.yaml-sourced one above). Order below is rough preference
# (direct providers preferred over OR routing for the same model family).
if [ -z "${HERMES_DEFAULT_MODEL:-}" ] && [ -z "${HERMES_INFERENCE_PROVIDER:-}" ]; then
  if   [ -n "${HERMES_API_KEY:-}" ] || [ -n "${NOUS_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="nousresearch/hermes-4-70b"
  elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="anthropic/claude-sonnet-4-5"
  elif [ -n "${OPENAI_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="openai/gpt-4o"
  elif [ -n "${MINIMAX_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="minimax/MiniMax-M2.7-highspeed"
  elif [ -n "${MINIMAX_CN_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="minimax-cn/abab6.5-chat"
  elif [ -n "${GEMINI_API_KEY:-}" ] || [ -n "${GOOGLE_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="gemini/gemini-2.0-flash"
  elif [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="deepseek/deepseek-chat"
  elif [ -n "${KIMI_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="kimi/kimi-k2"
  elif [ -n "${OPENROUTER_API_KEY:-}" ]; then
    HERMES_DEFAULT_MODEL="nousresearch/hermes-4-70b"  # routes via OR
  else
    # No provider key at all — keep the historical fallback so the
    # error surfaces as the same "No LLM provider configured" message
    # that operators are familiar with (rather than swapping it for a
    # different obscure error).
    HERMES_DEFAULT_MODEL="nousresearch/hermes-4-70b"
  fi
  echo "[start.sh] HERMES_DEFAULT_MODEL was unset; auto-selected '${HERMES_DEFAULT_MODEL}' from available API keys"
fi

DEFAULT_MODEL="${HERMES_DEFAULT_MODEL}"
# Derive provider from model slug prefix — shared with install.sh via
# scripts/derive-provider.sh so Docker + bare-host paths match.
# Dockerfile COPYs scripts/ to /app/scripts; fall back to /scripts
# for dev environments that run start.sh with a different WORKDIR.
DERIVE_SCRIPT="/app/scripts/derive-provider.sh"
[ -f "$DERIVE_SCRIPT" ] || DERIVE_SCRIPT="/scripts/derive-provider.sh"
HERMES_DEFAULT_MODEL="${DEFAULT_MODEL}" . "$DERIVE_SCRIPT"

# --- OpenAI bridge: custom provider + chat_completions api_mode ---
# Symmetric with install.sh. See install.sh for the full explanation.
# hermes has NO native "openai" provider — bridge must use custom+
# api_mode=chat_completions to get the OpenAI-compat /v1/chat/completions
# path (not /v1/responses with encrypted_content, which 400s on gpt-4o).
if [ "${PROVIDER}" = "custom" ] && [ -n "${OPENAI_API_KEY:-}" ] && [ -z "${HERMES_CUSTOM_BASE_URL:-}" ] && [ -z "${HERMES_CUSTOM_API_KEY:-}" ]; then
  export HERMES_CUSTOM_BASE_URL="https://api.openai.com/v1"
  export HERMES_CUSTOM_API_KEY="${OPENAI_API_KEY}"
  export HERMES_CUSTOM_API_MODE="chat_completions"
  DEFAULT_MODEL="${DEFAULT_MODEL#openai/}"
  echo "[start.sh] bridged OPENAI_API_KEY → custom provider @ api.openai.com (api_mode=chat_completions, model=${DEFAULT_MODEL})"
fi

{
  echo "# Seeded by molecule template-hermes start.sh. Customize via"
  echo "# \`hermes config edit\` or by editing this file directly."
  echo "# start.sh rewrites model.default + model.provider on every"
  echo "# boot from HERMES_DEFAULT_MODEL / HERMES_INFERENCE_PROVIDER env."
  echo "model:"
  echo "  default: \"${DEFAULT_MODEL}\""
  echo "  provider: \"${PROVIDER}\""
  # For custom provider (or its aliases lmstudio/ollama/vllm/llamacpp),
  # let operators pipe the base_url and api_key through env. Useful for
  # pointing at a non-OpenRouter OpenAI-compat endpoint (OpenAI direct,
  # LiteLLM gateway, LM Studio, local vLLM, etc.).
  if [ -n "${HERMES_CUSTOM_BASE_URL:-}" ]; then
    echo "  base_url: \"${HERMES_CUSTOM_BASE_URL}\""
  fi
  if [ -n "${HERMES_CUSTOM_API_KEY:-}" ]; then
    echo "  api_key: \"${HERMES_CUSTOM_API_KEY}\""
  fi
  # api_mode gates hermes custom-provider request shape:
  #   chat_completions  → /v1/chat/completions (OpenAI-compat)
  #   codex_responses   → /v1/responses + encrypted_content (o1 only)
  if [ -n "${HERMES_CUSTOM_API_MODE:-}" ]; then
    echo "  api_mode: \"${HERMES_CUSTOM_API_MODE}\""
  fi
  # --- Molecule A2A platform plugin ---
  # Loaded into hermes via the hermes_agent.plugins entry point baked
  # into the image (see Dockerfile). When enabled, hermes opens a
  # localhost HTTP listener on MOLECULE_A2A_PLATFORM_PORT; molecule-runtime
  # POSTs A2A peer messages there and gets agent replies back via the
  # callback_url. Independent of the OpenAI-compat api-server platform
  # on :8642 — both run side-by-side. The runtime adapter still uses
  # the api-server bridge today; switching to the plugin path is a
  # separate adapter.py change (post-demo).
  if [ "${MOLECULE_A2A_PLATFORM_ENABLED:-true}" = "true" ]; then
    # Default the plugin's callback URL to the executor's reply
    # server (started by adapter.create_executor → executor.start()).
    # Operators can pin a custom URL via env if molecule-runtime is
    # extended to host /a2a/reply itself.
    DEFAULT_CALLBACK="http://${MOLECULE_A2A_CALLBACK_HOST:-127.0.0.1}:${MOLECULE_A2A_CALLBACK_PORT:-8646}/a2a/reply"
    echo "platforms:"
    echo "  molecule-a2a:"
    echo "    enabled: true"
    echo "    extra:"
    echo "      host: \"${MOLECULE_A2A_PLATFORM_HOST:-127.0.0.1}\""
    echo "      port: ${MOLECULE_A2A_PLATFORM_PORT:-8645}"
    echo "      callback_url: \"${MOLECULE_A2A_PLATFORM_CALLBACK_URL:-${DEFAULT_CALLBACK}}\""
    if [ -n "${MOLECULE_A2A_PLATFORM_SHARED_SECRET:-}" ]; then
      echo "      shared_secret: \"${MOLECULE_A2A_PLATFORM_SHARED_SECRET}\""
    fi
  fi
} >"$HERMES_CONFIG"
chown agent:agent "$HERMES_CONFIG"

# --- Start hermes gateway in the background ---
# `hermes gateway` reads ~/.hermes/.env at startup. We override HOME to
# /tmp so the lookup resolves to /tmp/.hermes/.env (writable; matches
# HERMES_HOME above). The hermes binary is on PATH via /home/agent/.local/bin
# (set in Dockerfile) — that location is read-only under T1 sandbox but
# binary lookup only needs read.
# Use bash -c (not -lc) since we no longer want the login-shell HOME-driven
# defaults; we're explicitly setting PATH + HOME inline.
nohup gosu agent env HOME=/tmp PATH="/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    bash -c "cd /tmp && hermes gateway" \
    >>"$LOG_FILE" 2>&1 &
GATEWAY_PID=$!

# --- Wait for :8642 readiness ---
# Max 120s — enough for a cold gateway boot including first-time DB
# migrations and session-store init. Longer waits should surface as a
# provisioning failure upstream rather than silently holding the container.
READY_TIMEOUT=120
for _ in $(seq 1 $READY_TIMEOUT); do
  if curl -fsS "http://127.0.0.1:${API_SERVER_PORT:-8642}/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "[start.sh] hermes gateway exited during boot. Last log lines:" >&2
    tail -40 "$LOG_FILE" >&2
    exit 1
  fi
  sleep 1
done

if ! curl -fsS "http://127.0.0.1:${API_SERVER_PORT:-8642}/health" >/dev/null 2>&1; then
  echo "[start.sh] hermes gateway failed to reach /health within ${READY_TIMEOUT}s." >&2
  tail -80 "$LOG_FILE" >&2
  exit 1
fi

echo "[start.sh] hermes gateway ready on :${API_SERVER_PORT:-8642} (pid ${GATEWAY_PID})"

# --- Exec molecule-runtime on :8000 ---
# From here on, every A2A message the platform sends gets proxied
# through executor.py → :8642 → hermes-agent.
exec molecule-runtime
