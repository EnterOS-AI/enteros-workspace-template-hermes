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

HERMES_HOME="/home/agent/.hermes"
ENV_FILE="${HERMES_HOME}/.env"
HERMES_CONFIG="${HERMES_HOME}/config.yaml"
LOG_FILE="/var/log/hermes-gateway.log"

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
DEFAULT_MODEL="${HERMES_DEFAULT_MODEL:-nousresearch/hermes-4-70b}"
# Derive provider from model slug prefix — shared with install.sh via
# scripts/derive-provider.sh so Docker + bare-host paths match.
# Dockerfile COPYs scripts/ to /app/scripts; fall back to /scripts
# for dev environments that run start.sh with a different WORKDIR.
DERIVE_SCRIPT="/app/scripts/derive-provider.sh"
[ -f "$DERIVE_SCRIPT" ] || DERIVE_SCRIPT="/scripts/derive-provider.sh"
HERMES_DEFAULT_MODEL="${DEFAULT_MODEL}" . "$DERIVE_SCRIPT"
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
} >"$HERMES_CONFIG"
chown agent:agent "$HERMES_CONFIG"

# --- Start hermes gateway in the background ---
# `hermes gateway` reads ~/.hermes/.env at startup. We run it as the
# agent user via gosu so memory/skills land in the agent-owned home.
# `bash -lc` forces a login shell so .profile / .bashrc add ~/.local/bin
# to PATH (that's where install.sh symlinks the hermes binary).
nohup gosu agent bash -lc "cd /home/agent && hermes gateway" \
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
