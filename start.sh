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

# --- Write hermes-agent's .env ---
# API_SERVER_ENABLED must be true and the bearer must match. Provider
# keys (HERMES_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY /
# OPENAI_API_KEY / GEMINI_API_KEY / MINIMAX_API_KEY) are forwarded from
# the container env — hermes-agent will pick the right one based on the
# model selected via `hermes model`.
sudo -u agent mkdir -p "$HERMES_HOME"
sudo -u agent tee "$ENV_FILE" >/dev/null <<EOF
API_SERVER_ENABLED=true
API_SERVER_KEY=${API_SERVER_KEY}
API_SERVER_HOST=${API_SERVER_HOST:-127.0.0.1}
API_SERVER_PORT=${API_SERVER_PORT:-8642}
${HERMES_API_KEY:+HERMES_API_KEY=${HERMES_API_KEY}}
${OPENROUTER_API_KEY:+OPENROUTER_API_KEY=${OPENROUTER_API_KEY}}
${ANTHROPIC_API_KEY:+ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}}
${OPENAI_API_KEY:+OPENAI_API_KEY=${OPENAI_API_KEY}}
${GEMINI_API_KEY:+GEMINI_API_KEY=${GEMINI_API_KEY}}
${MINIMAX_API_KEY:+MINIMAX_API_KEY=${MINIMAX_API_KEY}}
EOF

# --- Start hermes gateway in the background ---
# `hermes gateway` reads ~/.hermes/.env at startup. We run it as the
# agent user so memory/skills land in the agent-owned home.
nohup sudo -u agent -E bash -lc "hermes gateway" >>"$LOG_FILE" 2>&1 &
GATEWAY_PID=$!

# --- Wait for :8642 readiness ---
# Max 60s — enough for a cold gateway boot including first-time DB
# migrations. Longer waits should surface as a provisioning failure
# upstream rather than silently holding the container.
for _ in $(seq 1 60); do
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
  echo "[start.sh] hermes gateway failed to reach /health within 60s." >&2
  tail -80 "$LOG_FILE" >&2
  exit 1
fi

echo "[start.sh] hermes gateway ready on :${API_SERVER_PORT:-8642} (pid ${GATEWAY_PID})"

# --- Exec molecule-runtime on :8000 ---
# From here on, every A2A message the platform sends gets proxied
# through executor.py → :8642 → hermes-agent.
exec molecule-runtime
