#!/usr/bin/env bash
# install.sh — set up hermes-agent on a bare-host workspace (EC2 /
# bare-metal / any OS-level install). Runs as the workspace's runtime
# user (typically `ubuntu` on EC2) AFTER molecule-ai-workspace-runtime
# has been pip-installed and this repo's *.py adapter files have been
# copied into site-packages, BEFORE molecule-runtime is started.
#
# This is the symmetric twin of start.sh. start.sh is the entrypoint
# of the Docker image used for local dev (`docker compose up`).
# install.sh is what the SaaS EC2 provisioner calls on the host.
#
# Both do the same high-level work:
#   1. Install the real hermes-agent from NousResearch/hermes-agent
#   2. Seed ~/.hermes/.env (provider keys, API_SERVER_*)
#   3. Seed ~/.hermes/config.yaml (default model + provider)
#   4. Start `hermes gateway` in the background
#   5. Wait until :8642 /health returns 200
#
# Architectural context: each workspace template ships both recipes
# because the control plane picks different code paths depending on
# backend. See internal/product/designs/workspace-backends.md for the
# manifest-driven backend-selection design that subsumes this dual
# setup.
#
# Idempotent: safe to re-run. Kills any prior gateway process for
# this user before starting a fresh one.

set -euo pipefail

HERMES_HOME="$HOME/.hermes"
LOG_FILE="/var/log/hermes-gateway.log"
API_SERVER_PORT="${API_SERVER_PORT:-8642}"
API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"

echo "[install.sh] hermes bare-host setup starting (user=$USER, home=$HOME)"

# --- System deps (idempotent) ---
# hermes-agent installer pulls a Node 22 .tar.xz and builds some
# Python deps from source. Ubuntu EC2 AMI ships without xz or gcc.
if ! command -v xz >/dev/null 2>&1 || ! command -v gcc >/dev/null 2>&1; then
  echo "[install.sh] installing system deps (xz-utils + build-essential)..."
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    curl ca-certificates git xz-utils build-essential
fi

# --- Install hermes-agent (only if not already present) ---
# Installer places `hermes` at ~/.local/bin/hermes (symlink to
# ~/.hermes/hermes-agent/venv/bin/hermes). --skip-setup avoids the
# interactive wizard.
if ! command -v hermes >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/hermes" ]; then
  echo "[install.sh] installing hermes-agent from NousResearch..."
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
    | bash -s -- --skip-setup
fi
export PATH="$HOME/.local/bin:$PATH"

# --- Ensure hermes home exists ---
mkdir -p "$HERMES_HOME"

# --- Generate API_SERVER_KEY if not already set in env ---
# hermes-agent requires a bearer for the api-server platform. The
# molecule_runtime adapter (executor.py) reads this same var at
# request time to auth against the gateway.
if [ -z "${API_SERVER_KEY:-}" ]; then
  API_SERVER_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)"
  export API_SERVER_KEY
fi

# --- Write hermes-agent .env ---
# Every provider key the workspace's process env carries is forwarded.
# CP's provisioner injects these from Secrets Manager + the per-tenant
# shared secret bundle before this script runs.
cat >"$HERMES_HOME/.env" <<EOF
API_SERVER_ENABLED=true
API_SERVER_KEY=${API_SERVER_KEY}
API_SERVER_HOST=${API_SERVER_HOST}
API_SERVER_PORT=${API_SERVER_PORT}
${HERMES_INFERENCE_PROVIDER:+HERMES_INFERENCE_PROVIDER=${HERMES_INFERENCE_PROVIDER}}
${HERMES_AUXILIARY_PROVIDER:+HERMES_AUXILIARY_PROVIDER=${HERMES_AUXILIARY_PROVIDER}}
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
${COPILOT_GITHUB_TOKEN:+COPILOT_GITHUB_TOKEN=${COPILOT_GITHUB_TOKEN}}
${GH_TOKEN:+GH_TOKEN=${GH_TOKEN}}
EOF
chmod 600 "$HERMES_HOME/.env"

# --- Write hermes-agent config.yaml ---
# Unconditional overwrite — the hermes installer drops its
# cli-config.yaml.example here as config.yaml which defaults to
# anthropic/claude-opus-4.6 + provider:auto. Our bridge needs
# deterministic routing.
PROVIDER="${HERMES_INFERENCE_PROVIDER:-auto}"
DEFAULT_MODEL="${HERMES_DEFAULT_MODEL:-nousresearch/hermes-4-70b}"
{
  echo "# Seeded by template-hermes install.sh on $(date -u -Iseconds)"
  echo "# Rewritten each boot from HERMES_DEFAULT_MODEL + HERMES_INFERENCE_PROVIDER env."
  echo "model:"
  echo "  default: \"${DEFAULT_MODEL}\""
  echo "  provider: \"${PROVIDER}\""
  if [ -n "${HERMES_CUSTOM_BASE_URL:-}" ]; then
    echo "  base_url: \"${HERMES_CUSTOM_BASE_URL}\""
  fi
  if [ -n "${HERMES_CUSTOM_API_KEY:-}" ]; then
    echo "  api_key: \"${HERMES_CUSTOM_API_KEY}\""
  fi
} >"$HERMES_HOME/config.yaml"

# --- Prepare gateway log ---
# /var/log needs root to create the file; chown to runtime user so
# the gateway (running as that user) can append to it.
sudo touch "$LOG_FILE"
sudo chown "$USER:$USER" "$LOG_FILE"

# --- Kill prior gateway, start fresh ---
if pgrep -u "$USER" -f "hermes gateway" >/dev/null 2>&1; then
  echo "[install.sh] killing prior hermes gateway process(es) for $USER..."
  pkill -u "$USER" -f "hermes gateway" 2>/dev/null || true
  sleep 2
fi

echo "[install.sh] starting hermes gateway in background..."
# `bash -lc` forces a login shell so .bashrc's PATH export is picked
# up for the `hermes` binary. `cd $HOME` is defensive — hermes writes
# relative state if CWD is unusual.
nohup bash -lc "cd $HOME && exec hermes gateway" >>"$LOG_FILE" 2>&1 &
GATEWAY_PID=$!

# --- Wait for :8642 readiness (max 120s) ---
READY_TIMEOUT=120
for _ in $(seq 1 $READY_TIMEOUT); do
  if curl -fsS "http://${API_SERVER_HOST}:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
    echo "[install.sh] hermes gateway ready on :${API_SERVER_PORT} (pid ${GATEWAY_PID})"
    exit 0
  fi
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "[install.sh] hermes gateway exited during boot. Last log lines:" >&2
    tail -40 "$LOG_FILE" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "[install.sh] hermes gateway failed to reach /health within ${READY_TIMEOUT}s." >&2
tail -80 "$LOG_FILE" >&2 || true
exit 1
