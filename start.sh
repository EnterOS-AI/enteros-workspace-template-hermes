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

# shellcheck source=scripts/process-liveness.sh
. /app/scripts/process-liveness.sh

# Source persistent workspace secrets BEFORE anything else that might need them.
# /configs is volume-mounted from the host so this survives container restart.
if [ -f /configs/secrets.d/load.sh ]; then
  # shellcheck disable=SC1091
  . /configs/secrets.d/load.sh
fi

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
  exec gosu agent env HOME=/tmp CONFIGS_DIR=/configs molecule-runtime
fi

# --- Make /configs agent-owned (fleet contract) ---
# The /configs volume is created by Docker/the provisioner as root, but
# molecule_runtime/configs_dir.py's documented contract is that /configs
# is "owned by the agent user" — and the agent-context molecule-mcp
# server (started below as `gosu agent`) needs to READ /configs/.auth_token
# (the platform bearer) and WRITE .platform_inbound_secret rotations.
#
# Without this chown, configs_dir.resolve() running as the agent user
# fails the `os.access('/configs', W_OK)` test and silently falls back to
# $HOME/.molecule-workspace (= /tmp/.molecule-workspace under HOME=/tmp),
# where there is no .auth_token. platform_auth.get_token() then returns
# None, auth_headers() sends NO Authorization header, and EVERY
# `list_peers` / A2A MCP tool call 401s with the canned
# "restart the workspace usually re-mints it" message — confidently
# wrong, because the correct token IS on disk at /configs/.auth_token,
# just unreadable by the agent. (Root-context callers like
# molecule-runtime's heartbeat were unaffected, which is why the
# authenticated registry+heartbeat chain looked healthy while the
# literal list_peers MCP path — never exercised by HERMES-FLEET-VERIFIED
# — was broken.)
#
# This mirrors the established fleet pattern: the claude-code template's
# entrypoint.sh does the same `chown -R agent:agent /configs` when
# running as root, with a comment explicitly naming ".auth_token
# rotation" as a reason. start.sh runs as root here (before any gosu),
# so the chown takes effect for the agent-context children below.
#
# T4 atomic-co-sequencing contract (RFC internal#456 §10): the T4
# escalation leg (sudo NOPASSWD + docker group, baked in the
# Dockerfile) is ADDITIVE. The agent still runs uid-1000 (the
# `exec gosu agent` below is UNCHANGED) and /configs/.auth_token MUST
# remain agent-owned — escalation must NOT regress the Hermes
# list_peers-401 token-ownership class. This chown -R is the
# agent-ownership half of that contract; the Layer-3 conformance gate
# asserts owner_uid==1000 on the running container alongside the
# host-root-reach assertion. Mirrors claude-code entrypoint.sh (12dd604).
if [ "$(id -u)" = "0" ]; then
  chown -R agent:agent /configs 2>/dev/null || true
  # /workspace is the DURABLE volume (mailbox kernel state: inbox cursor,
  # delegation tombstones, goal-state, task ledger, memory — plus chat
  # uploads). Docker ships named volumes root:755; the provisioner contract
  # (workspace-server provisioner.go: "the entrypoint starts as root, chowns
  # /configs and /workspace, then drops to agent") was only half-implemented
  # here — /configs was chowned, /workspace was NOT, so every uid-1000 kernel
  # write failed (durability guard: UNWRITABLE; idle-digest providers
  # PermissionError; goal_set silently unable to persist). core#4295.
  #
  # GUARDED on current ownership: only claim a ROOT-owned tree (the broken-era
  # named-volume shape — the heal fires exactly once, then steady-state boots
  # skip the full tree walk). Never touch a /workspace owned by anyone else:
  # in WorkspacePath mode it is an operator's real HOST directory, and
  # recursively re-owning their files to uid 1000 would be a silent, harmful
  # host mutation. Everything below runs uid-1000 via gosu, so an agent-owned
  # tree never drifts back to root.
  if [ "$(stat -c %u /workspace 2>/dev/null)" = "0" ]; then
    chown -R agent:agent /workspace 2>/dev/null || true
  fi
fi

HERMES_HOME="/tmp/.hermes"
# Persist hermes state across container RECREATES (plugin installs and
# platform restarts recreate the container): every process resolves the state
# dir as ~/.hermes with HOME=/tmp (gateway, hermes CLI, the runtime's config
# render), so rather than re-plumb HOME everywhere, /tmp/.hermes becomes a
# SYMLINK onto the persisted /configs volume. Without this the session store
# (state.db, sessions/) died with the old container and the agent woke
# amnesiac -- "session history is empty" (2026-07-19, lost the lark-install
# task mid-conversation; executor.py's history re-attach comment tracks the
# same gap as "separate task: move HERMES_HOME to a workspace volume").
install -d -o agent -g agent /configs/.hermes
rm -rf /tmp/.hermes 2>/dev/null || true
ln -sfn /configs/.hermes /tmp/.hermes
# A container (re)start is ALWAYS a platform-orchestrated restart from the
# gateway's point of view (docker stop/recreate — plugin installs, admin
# restarts, host reboots), never a hermes-internal crash. Upstream's marker
# for exactly this class ("hermes update", "gateway restart") makes startup
# SKIP suspend_recently_active(); without it, a reprovision that lands
# mid-turn suspends the session and the FIRST post-boot message force-resets
# it (end_reason=session_reset) — the agent wakes amnesiac and drops the
# task it promised to resume (2026-07-21, lark-install flow lost again
# even WITH the /configs symlink persisting the transcripts). The gateway
# consumes (unlinks) the marker on startup, so an in-container gateway
# crash-respawn still gets the normal suspension treatment, and the
# stuck-loop guard (.restart_failure_counts, 3+ restarts while active)
# runs unconditionally either way — real crash loops still get wiped.
install -o agent -g agent /dev/null /tmp/.hermes/.clean_shutdown
ENV_FILE="${HERMES_HOME}/.env"
HERMES_CONFIG="${HERMES_HOME}/config.yaml"
# Log files live under HERMES_HOME (agent-owned via `install -d -o agent`
# below) — NOT a bare /tmp path. See the comment block above the log-file
# `install` calls for the full rationale; the short version: with
# `set -euo pipefail`, `nohup gosu agent ... >> /tmp/some.log` is racy
# because the redirect open() is done by the ROOT parent shell BEFORE
# `gosu` execs the agent user. If a previous boot left /tmp/some.log
# owned by `agent` (or the file is created by `touch` then `chown`'d
# in two separate syscalls), the parent `>>` open can land between the
# touch and the chown — root-owned 644 file → EPERM on the next boot's
# `>>` open → set -e kills start.sh → gateway never spawns → container
# crashloops. Putting logs under HERMES_HOME (which `install -d -o agent`
# creates with agent ownership in a single atomic syscall) plus using
# `install -m 644 -o agent -g agent /dev/null` for the files themselves
# closes the race window entirely.
LOG_FILE="${HERMES_HOME}/gateway.log"

# --- Generate a per-container API_SERVER_KEY ---
# hermes-agent requires a bearer token on the api-server platform. We
# generate a random value per boot and inject it into both processes via
# env — molecule_runtime's executor reads the same var at request time.
if [ -z "${API_SERVER_KEY:-}" ]; then
  API_SERVER_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)"
  export API_SERVER_KEY
fi

# Create HERMES_HOME up front (agent-owned) so the log-file installs
# below land inside an agent-owned dir. MUST happen before any gateway
# log-file or MCP-log-file `install` calls.
install -d -o agent -g agent "$HERMES_HOME"

# --- Persona -> SOUL.md graft -------------------------------------------------
# The platform delivers the workspace persona to /configs (the concierge's
# composed config grafts it at prompts/concierge.md; plain workspaces use
# system-prompt.md). The hermes DAEMON reads its identity from
# ${HERMES_HOME}/SOUL.md and knows nothing about prompt_files -- without this
# graft it boots on the stock Hermes soul and answers as "Hermes", not its
# role (the 2026-07-19 "I'm Hermes" concierge-identity incident). The
# daemon's _ensure_default_soul_md only seeds SOUL.md when ABSENT, so our
# pre-start install wins; re-installing on every boot keeps a persona update
# effective after restart.
for persona in /configs/prompts/concierge.md /configs/system-prompt.md; do
  if [ -s "$persona" ]; then
    install -m 644 -o agent -g agent "$persona" "${HERMES_HOME}/SOUL.md"
    echo "[start.sh] grafted persona ${persona} -> ${HERMES_HOME}/SOUL.md"
    break
  fi
done

# --- Install log files atomically (race-free) ---
# Regression fix re-applied 2026-05-15: a previous build (image
# sha-7669af2, built 13:31 UTC) crashloops in prod with:
#   /usr/local/bin/start.sh: line N: /tmp/molecule-mcp-server.log: Permission denied
#   /usr/local/bin/start.sh: line N: /tmp/hermes-gateway.log: Permission denied
# The bug: `set -euo pipefail` + `touch FILE && chown agent FILE; ...
# nohup gosu agent ... >> FILE` is racy. The `>>` redirect is opened
# by the ROOT parent shell BEFORE `gosu` execs agent. With separate
# touch + chown syscalls (or a stale /tmp file from a prior boot in a
# different uid namespace), the open() can return EPERM, set -e kills
# the script, and the gateway never registers with the CP → workspace
# state=failed at 720s.
# The May-6 working image (sha256:0f8e83f4…) used `install -m 644
# -o agent -g agent /dev/null "$LOG_FILE"` — a single atomic syscall
# that creates the file with the final perms in one shot — and located
# logs inside HERMES_HOME (agent-owned). Re-applying that fix here.
install -m 644 -o agent -g agent /dev/null "$LOG_FILE"

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
# Provider selection is appended after platform-routing translation below.
# Auxiliary model defaults — used by vision, web summarization, MoA.
${HERMES_AUXILIARY_PROVIDER:+HERMES_AUXILIARY_PROVIDER=${HERMES_AUXILIARY_PROVIDER}}
# ── Primary inference providers (keyed) ───────────────────────
# NOTE: HERMES_API_KEY intentionally NOT forwarded — upstream uses it only for
# the TUI gateway bridge, not as an LLM credential. Provider keys go below
# (NOUS_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY, …).
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
# + HERMES_INFERENCE_MODEL env, or by editing config.yaml at runtime
# inside the container.
# Pull HERMES_INFERENCE_MODEL/HERMES_DEFAULT_MODEL + HERMES_INFERENCE_PROVIDER
# out of /configs/config.yaml (canvas Config tab values, written by CP
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

# --- Honor the CP-injected unified workspace model (fleet contract) ---
# The control-plane provisioner injects the ONE resolved model id the
# workspace should run as MOLECULE_MODEL (SSOT, preferred) == MODEL (legacy)
# — molecule-controlplane/internal/provisioner/local_docker_workspace.go
# setUnifiedModel/unifiedWorkspaceModel. Every OTHER runtime (claude-code,
# openclaw, codex) selects on MOLECULE_MODEL, but hermes historically read
# ONLY HERMES_INFERENCE_MODEL / HERMES_DEFAULT_MODEL / config.yaml
# runtime_config.model — so it IGNORED the CP-resolved model and fell through
# to the hardcoded `nousresearch/hermes-4-70b` key-presence default below.
#
# For a PLATFORM-MANAGED CONCIERGE (kind=platform) that is fatal: the concierge
# is Rule-#13-forbidden from pinning runtime_config.model in its config.yaml
# (molecule-controlplane/internal/providers/template_model_gate.go), so
# load-workspace-config.sh finds NO model, no HERMES_* env is set, and the
# hardcoded fallback fires. `nousresearch/hermes-4-70b` has NO llm_price_catalog
# row (it routes to the proxy's default `openai` arm), so the fail-closed LLM
# proxy 422s EVERY tool-use turn ("model has no price catalog entry"). The
# concierge is supposed to INHERIT the platform SSOT default
# (MOLECULE_LLM_DEFAULT_MODEL, currently minimax/MiniMax-M2.7 — catalogued),
# which the CP already delivers as MOLECULE_MODEL/MODEL.
#
# Fix: map the CP unified model into HERMES_DEFAULT_MODEL when no explicit
# hermes selection exists yet, mirroring the CP's own precedence
# (MOLECULE_MODEL → MODEL). An operator HERMES_* env or a canvas config.yaml
# pick still wins (set above / by load-workspace-config.sh). The key-presence
# guesses below become a true last resort — only reached when the CP passed no
# unified model at all (dev / non-CP boots). No hardcoded model here: the value
# is the SSOT id the CP resolved, so the catalog/default stays single-sourced.
if [ -z "${HERMES_INFERENCE_MODEL:-}" ] && [ -z "${HERMES_DEFAULT_MODEL:-}" ] && [ -z "${HERMES_INFERENCE_PROVIDER:-}" ]; then
  if   [ -n "${MOLECULE_MODEL:-}" ]; then
    HERMES_DEFAULT_MODEL="${MOLECULE_MODEL}"
    echo "[start.sh] inheriting CP unified model MOLECULE_MODEL='${HERMES_DEFAULT_MODEL}'"
  elif [ -n "${MODEL:-}" ]; then
    HERMES_DEFAULT_MODEL="${MODEL}"
    echo "[start.sh] inheriting CP unified model MODEL='${HERMES_DEFAULT_MODEL}'"
  fi
fi

# Pick a default model. The fallback used to be `nousresearch/hermes-4-70b`
# unconditionally, which derives PROVIDER=openrouter when no Nous key is
# present — and if OPENROUTER_API_KEY isn't set either, hermes-agent boots
# with a config that points at a provider with no usable key, then 500s
# at request time with "No LLM provider configured". Surfaces as a real
# user-facing error whenever a workspace is provisioned with a single
# provider key (e.g. just MINIMAX_API_KEY) but no explicit model
# selection — the canvas's "set key, save, send" flow.
#
# Fix: when neither model env var is set and HERMES_INFERENCE_PROVIDER
# is unset, pick the default model based on which API key is actually
# present in env. Keeps the behaviour-when-everything-is-set unchanged.
# Order below is rough preference (direct providers preferred over OR
# routing for the same model family).
#
# We accept BOTH HERMES_INFERENCE_MODEL (upstream's actual env var, see
# NousResearch/hermes-agent website/docs/reference/environment-variables.md)
# AND HERMES_DEFAULT_MODEL (legacy name we invented before 2026-05).
# Workspace-server still writes the legacy name during the migration
# window — accepting both keeps boots green until that's fixed. Once
# workspace-server switches over, drop the HERMES_DEFAULT_MODEL fallback.
if [ -z "${HERMES_INFERENCE_MODEL:-}" ] && [ -z "${HERMES_DEFAULT_MODEL:-}" ] && [ -z "${HERMES_INFERENCE_PROVIDER:-}" ]; then
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
  echo "[start.sh] no model env was set; auto-selected '${HERMES_DEFAULT_MODEL}' from available API keys"
fi

# `:-` on the inner expansion so this is safe under `set -u` even when the
# key-presence block above was gated off (e.g. HERMES_INFERENCE_PROVIDER set)
# and never assigned HERMES_DEFAULT_MODEL — previously that dereferenced an
# unbound var and crashed the container with a cryptic error.
DEFAULT_MODEL="${HERMES_INFERENCE_MODEL:-${HERMES_DEFAULT_MODEL:-}}"

# --- Platform-managed model guard (fail-loud, OUTCOME-based) ---
# On a platform-routed workspace (MOLECULE_RESOLVED_PROVIDER=platform — the SSOT
# signal core injects; derive-platform-llm.sh keys on the SAME value) the tenant
# BYOK keys are STRIPPED and the CP is contracted to deliver the resolved model.
# If model selection nonetheless landed on the BYOK default
# `nousresearch/hermes-4-70b` (the key-presence fallback with no keys present) or
# on nothing at all, that model is deliberately unpriced (IsPlatform:false) and
# the platform LLM proxy fail-closes it 422 "model has no price catalog entry" on
# EVERY turn — a silent, minutes-long retry loop rather than an obvious failure
# (the exact class that took a full staging repro to diagnose). Refuse loudly.
#
# This tests the FINAL resolved DEFAULT_MODEL (not intermediate HERMES_* proxies)
# and keys ONLY on the authoritative MOLECULE_RESOLVED_PROVIDER, so it can never
# false-fire: a workspace that actually resolved a model (whatever path delivered
# it) has a non-default DEFAULT_MODEL and is left alone, and a byok arm (or a
# stale legacy LLM_PROVIDER=platform) is never mis-classified as platform. It
# fires ONLY on the exact unpriced BYOK default — the empty-model case is a
# distinct concern already made non-fatal by the `:-` on DEFAULT_MODEL above.
case "${MOLECULE_RESOLVED_PROVIDER:-}" in
  platform)
    if [ "${DEFAULT_MODEL}" = "nousresearch/hermes-4-70b" ]; then
      echo "[start.sh] platform-managed hermes workspace fell through to the BYOK default 'nousresearch/hermes-4-70b'." >&2
      echo "[start.sh] That model is unpriced (IsPlatform:false), so the platform LLM proxy rejects it 422" >&2
      echo "[start.sh] 'model has no price catalog entry' on every turn. The control plane must inject the" >&2
      echo "[start.sh] resolved platform model (MOLECULE_MODEL). Refusing to boot on the unpriced default." >&2
      exit 1
    fi
    ;;
esac
# --- end platform-managed model guard ---

# Derive provider from model slug prefix — shared with install.sh via
# scripts/derive-provider.sh so Docker + bare-host paths match.
# Dockerfile COPYs scripts/ to /app/scripts; fall back to /scripts
# for dev environments that run start.sh with a different WORKDIR.
DERIVE_SCRIPT="/app/scripts/derive-provider.sh"
[ -f "$DERIVE_SCRIPT" ] || DERIVE_SCRIPT="/scripts/derive-provider.sh"
HERMES_INFERENCE_MODEL="${DEFAULT_MODEL}" . "$DERIVE_SCRIPT"

# --- Platform provider override ---
# When the workspace's resolved provider is `platform` (LLM_PROVIDER=platform /
# HERMES_INFERENCE_PROVIDER=platform / a platform/ model — NOT a billing-mode
# env), route ALL inference through the Molecule platform proxy (OpenAI-compat
# surface) regardless of the model's natural vendor prefix — for the platform
# arm the tenant has no BYOK key (workspace-server strips them) and the proxy
# owns the keys + billing. Sourced AFTER derive-provider.sh so it wins. Fails
# closed (exit 1) if provider==platform but no platform base URL is present.
PLATFORM_LLM_SCRIPT="/app/scripts/derive-platform-llm.sh"
[ -f "$PLATFORM_LLM_SCRIPT" ] || PLATFORM_LLM_SCRIPT="/scripts/derive-platform-llm.sh"
if [ -f "$PLATFORM_LLM_SCRIPT" ]; then
  # shellcheck source=scripts/derive-platform-llm.sh
  . "$PLATFORM_LLM_SCRIPT" || {
    echo "[start.sh] platform-managed LLM routing failed — refusing to boot with an unroutable LLM config" >&2
    exit 1
  }
fi

# Hermes reloads this file with override=True before every request. Persist the
# translated value only after derive-platform-llm.sh has mapped the Molecule
# `platform` arm to Hermes's native `custom` provider.
if [ -n "${HERMES_INFERENCE_PROVIDER:-}" ]; then
  printf 'HERMES_INFERENCE_PROVIDER=%s\n' "${HERMES_INFERENCE_PROVIDER}" >>"$ENV_FILE"
fi

# --- OpenAI bridge: custom provider + chat_completions api_mode ---
# Symmetric with install.sh. See install.sh for the full explanation.
# hermes has NO native "openai" provider — bridge must use custom+
# api_mode=chat_completions to get the OpenAI-compat /v1/chat/completions
# path (not /v1/responses with encrypted_content, which 400s on gpt-4o).
if [ "${PROVIDER}" = "custom" ] && [ -n "${OPENAI_API_KEY:-}" ] && [ -z "${HERMES_CUSTOM_BASE_URL:-}" ] && [ -z "${HERMES_CUSTOM_API_KEY:-}" ]; then
  export HERMES_CUSTOM_BASE_URL="${OPENAI_BASE_URL:-${MOLECULE_LLM_BASE_URL:-https://api.openai.com/v1}}"
  export HERMES_CUSTOM_API_KEY="${OPENAI_API_KEY}"
  export HERMES_CUSTOM_API_MODE="chat_completions"
  DEFAULT_MODEL="${DEFAULT_MODEL#openai/}"
  echo "[start.sh] bridged OPENAI_API_KEY -> custom provider @ ${HERMES_CUSTOM_BASE_URL} (api_mode=chat_completions, model=${DEFAULT_MODEL})"
fi

# Defense-in-depth: when the resolved provider is platform (derive-platform-llm.sh
# set MOLECULE_PLATFORM_LLM_ACTIVE=1), refuse a HERMES_CUSTOM_BASE_URL that isn't
# the injected platform proxy base. Keyed on provider==platform, not a
# billing-mode env.
if [ "${MOLECULE_PLATFORM_LLM_ACTIVE:-}" = "1" ] && [ -n "${HERMES_CUSTOM_BASE_URL:-}" ]; then
  PLATFORM_OPENAI_BASE="${MOLECULE_LLM_BASE_URL:-${OPENAI_BASE_URL:-}}"
  if [ -z "${PLATFORM_OPENAI_BASE}" ] || [ "${HERMES_CUSTOM_BASE_URL}" != "${PLATFORM_OPENAI_BASE}" ]; then
    echo "[start.sh] refusing direct HERMES_CUSTOM_BASE_URL for the platform provider: ${HERMES_CUSTOM_BASE_URL}" >&2
    echo "[start.sh] use the Molecule platform proxy env (MOLECULE_LLM_BASE_URL/OPENAI_BASE_URL) instead." >&2
    exit 1
  fi
fi

# Molecule's BYOK registry uses `vendor:Model` for runtimes whose native UI
# already reserves `vendor/Model` for platform-managed models. Hermes's existing
# MiniMax slug grammar is slash-based, so normalize the BYOK colon form before
# writing hermes-agent config.
if [ "${PROVIDER}" = "minimax" ] && [[ "${DEFAULT_MODEL}" == minimax:* ]]; then
  BEFORE="${DEFAULT_MODEL}"
  DEFAULT_MODEL="minimax/${DEFAULT_MODEL#minimax:}"
  echo "[start.sh] normalized MiniMax BYOK model ${BEFORE} -> ${DEFAULT_MODEL}"
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
    # hermes >= 0.19 makes EXTERNAL (entry-point) plugins opt-in: only
    # names listed under plugins.enabled load; absent key = nothing
    # loads, and the gateway logs NOTHING about the skipped plugin
    # (first-boot hang 2026-07-23: molecule_a2a discovered but never
    # loaded, :8645 never bound, adapter.setup() failed, boot's TOOL
    # step waited forever). The name is the entry-point name from the
    # plugin's pyproject (molecule_a2a), not the platform key.
    echo "plugins:"
    echo "  enabled:"
    echo "    - molecule_a2a"
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
  # --- Molecule MCP server (canvas-agent tool affordance) ---
  # The MOLECULE_A2A_PLATFORM plugin above only handles INBOUND A2A
  # messages (peer-agent → hermes pipeline). It does NOT give hermes
  # outbound platform-tool affordances (list_peers, send_message_to_user,
  # delegate_task, commit_memory, recall_memory, …) when the user types
  # in the canvas chat. Without those tools, hermes responds coherently
  # to questions like "can you see your peers" with a generic "I don't
  # have visibility into other AI peers" — confidently wrong, because
  # the platform tools that WOULD give it visibility are running on
  # :9100 but hermes was never told about them.
  #
  # Wire hermes-agent's native MCP client at config.yaml time. The
  # molecule MCP server is started below (line ~340), bound to
  # 127.0.0.1:9100 inside the container; hermes connects on first
  # tool-discovery call from the gateway pipeline. 12 tools surface.
  #
  # Local probe confirms ✓ Connected + Tools discovered: 12 against the
  # `python3 -m molecule_runtime.a2a_mcp_server --transport http --port 9100`
  # server started below. Class-A canvas regression — without this
  # block hermes is platform-blind even though every other plumbing
  # layer is correct.
  echo "mcp_servers:"
  echo "  molecule:"
  echo "    url: \"http://127.0.0.1:${MOLECULE_MCP_PORT:-9100}/mcp\""
} >"$HERMES_CONFIG"
chown agent:agent "$HERMES_CONFIG"

# --- Start the Molecule A2A MCP HTTP server ---
# Exposes all Molecule platform tools (list_peers, delegate_task, recall_memory,
# commit_memory, send_message_to_user, get_workspace_info, …) over HTTP+SSE on
# :9100 so hermes-agent's native MCP client can connect to it.
# The executor.py chat-completions bridge also exposes the same tools as
# function-call tools; both paths complement each other.
# MCP_LOG lives under HERMES_HOME for the SAME race-free reason LOG_FILE
# does — see the LOG_FILE comment block at the top of this script. The
# old `MCP_LOG="/tmp/molecule-mcp-server.log"; touch + chown` pattern
# is the exact regression that put image sha-7669af2 into a crashloop
# (Permission denied on the >> redirect from the root parent shell).
MCP_LOG="${HERMES_HOME}/molecule-mcp-server.log"
install -m 644 -o agent -g agent /dev/null "$MCP_LOG"
# CONFIGS_DIR=/configs is REQUIRED here, not optional: the `env HOME=/tmp`
# form REPLACES the inherited environment, so even though /configs is now
# agent-owned (chowned above), configs_dir.resolve()'s second branch
# (`/configs exists AND os.access W_OK`) is the only thing that would
# save us — and relying on that implicit fallback is exactly what made
# this defect latent for so long. Setting CONFIGS_DIR explicitly makes
# the MCP server's token resolution deterministic and self-documenting:
# it takes configs_dir.resolve()'s FIRST (explicit-override) branch and
# can never silently fall back to /tmp/.molecule-workspace again.
nohup gosu agent env HOME=/tmp CONFIGS_DIR=/configs \
    python3 -m molecule_runtime.a2a_mcp_server --transport http --port 9100 \
    >>"$MCP_LOG" 2>&1 &
MCP_PID=$!
echo "[start.sh] Molecule A2A MCP HTTP server started (pid ${MCP_PID}) on :9100"

# --- Smoke: confirm MCP server responds to the JSON-RPC initialize ---
# Class-A regression guard. If the MCP server is up but the /mcp route
# is missing (server version mismatch, transport wired wrong) hermes
# will silently fail tool-discovery later and the canvas user will see
# generic "I don't have peer visibility" answers — exactly the symptom
# this PR is closing. Fail-fast here per
# feedback_chained_defects_in_never_tested_workflows: every never-fired
# smoke step is a latent defect waiting to surface in prod.
MCP_PORT="${MOLECULE_MCP_PORT:-9100}"
MCP_INIT_PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"start.sh-smoke","version":"1"}}}'
for _ in $(seq 1 15); do
  if curl -fsS -X POST -H 'Content-Type: application/json' \
       -H 'Accept: application/json, text/event-stream' \
       "http://127.0.0.1:${MCP_PORT}/mcp" -d "$MCP_INIT_PAYLOAD" \
       >/dev/null 2>&1; then
    echo "[start.sh] MCP server /mcp initialize OK"
    break
  fi
  if ! process_is_running "$MCP_PID"; then
    echo "[start.sh] MCP server exited before /mcp came up. Last log lines:" >&2
    tail -40 "$MCP_LOG" >&2
    exit 1
  fi
  sleep 1
done
if ! curl -fsS -X POST -H 'Content-Type: application/json' \
       -H 'Accept: application/json, text/event-stream' \
       "http://127.0.0.1:${MCP_PORT}/mcp" -d "$MCP_INIT_PAYLOAD" \
       >/dev/null 2>&1; then
  echo "[start.sh] MCP server /mcp initialize FAILED after 15s." >&2
  echo "[start.sh] hermes will boot without platform tools — refusing." >&2
  tail -40 "$MCP_LOG" >&2
  exit 1
fi

# --- Start hermes gateway in the background ---
# `hermes gateway` reads ~/.hermes/.env at startup. We override HOME to
# /tmp so the lookup resolves to /tmp/.hermes/.env (writable; matches
# HERMES_HOME above). The hermes binary is on PATH via /home/agent/.local/bin
# (set in Dockerfile) — that location is read-only under T1 sandbox but
# binary lookup only needs read.
# Use bash -c (not -lc) since we no longer want the login-shell HOME-driven
# defaults; we're explicitly setting PATH + HOME inline.
# BUSY_INPUT_MODE=queue: a message that arrives while a turn is running
# (the first-boot greeting, a long tool chain) QUEUES and cascades as the
# next turn instead of the default interrupt -- which aborted the running
# turn and sent the caller the "Interrupting current task..." ack AS THEIR
# ANSWER (the staging greeting e2e failure, 2026-07-20). Platform chat
# wants strict turn ordering; the canvas Stop button cancels via the task
# API, so no interrupt-by-chat capability is lost.
nohup gosu agent env HOME=/tmp HERMES_GATEWAY_BUSY_INPUT_MODE=queue PATH="/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
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
  if ! process_is_running "$GATEWAY_PID"; then
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

# --- MCP reconcile watcher (hermes >= 0.19 eager discovery) ---
# Extracted to scripts/mcp-reconcile-watch.sh so the watcher's drain /
# relaunch / multi-restart mechanics are hermetically unit-tested
# (scripts/test-mcp-reconcile-watch.sh) and exercised per-PR. Full
# rationale lives in that script's header. Backgrounded; survives the
# exec of molecule-runtime below.
MCPWATCH_CONFIG="$HERMES_CONFIG" MCPWATCH_GATEWAY_PID="$GATEWAY_PID" MCPWATCH_LAUNCH_CMD="exec gosu agent env HOME=/tmp HERMES_GATEWAY_BUSY_INPUT_MODE=queue PATH=/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -c 'cd /tmp && hermes gateway'" MCPWATCH_HEALTH_URL="http://127.0.0.1:${API_SERVER_PORT:-8642}/health" MCPWATCH_LOG_FILE="$LOG_FILE" MCPWATCH_MARKER="/tmp/.hermes/.clean_shutdown"   bash /usr/local/bin/mcp-reconcile-watch.sh &

# --- Smoke: confirm hermes sees the molecule MCP server in `mcp list` ---
# Belt-and-braces check: even if config.yaml has mcp_servers.molecule
# written above, a yaml-parse error or a hermes-side regression could
# silently drop the entry. Asserting hermes's view of its own config
# before allowing the workspace to come online closes the loop end-to-end.
# Soft-fail (log + continue): if the smoke step itself errors (busy CLI,
# transient stdin issue), we don't want to refuse boot — but a config
# without `molecule` in it IS a real failure, so we exit non-zero on that.
MCP_LIST_OUT=$(gosu agent env HOME=/tmp PATH="/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    timeout 10 hermes mcp list 2>&1 || true)
if echo "$MCP_LIST_OUT" | grep -q "^[[:space:]]*molecule[[:space:]]"; then
  echo "[start.sh] hermes mcp list shows 'molecule' wired"
else
  echo "[start.sh] FATAL: hermes mcp list did NOT show 'molecule' — platform tools unavailable" >&2
  echo "[start.sh] hermes mcp list output:" >&2
  echo "$MCP_LIST_OUT" >&2
  echo "[start.sh] config.yaml mcp_servers block:" >&2
  grep -A 5 '^mcp_servers' "$HERMES_CONFIG" >&2 || true
  exit 1
fi

# --- Exec molecule-runtime on :8000 ---
# From here on, every A2A message the platform sends gets proxied
# through executor.py → :8642 → hermes-agent.
#
# Run as the unprivileged `agent` (uid 1000) — NOT root. molecule-runtime's
# platform_auth.save_token() does os.open(.auth_token, O_WRONLY|O_CREAT|
# O_TRUNC, 0o600) at ~T+6s (after /registry/register returns). If this
# exec stays root, .auth_token lands root:root 0600 and the uid-1000 MCP
# server (line ~359) can't read it → get_token() None → no Authorization
# header → every list_peers / A2A MCP call 401s (task #162 P0 root cause).
# The line-~53 `chown -R agent:agent /configs` runs at T+0s, long before
# this T+6s write, so it is a timing no-op for .auth_token (proven: the
# same-root-writer .platform_inbound_secret also stays root:root post-chown).
# Mirror the exact env used for the gosu'd MCP server at line ~359 so token
# path resolution (configs_dir.resolve() explicit-override branch) stays
# deterministic. This also fixes .platform_inbound_secret (same writer).
#
# The runtime also owns declared-plugin source parsing, fetching, and atomic
# installation before adapter setup. Runtime 0.4 rejects unsafe dot/path install
# names and contains every resolved destination under /configs/plugins. Keep
# plugin installation out of this privileged boot script.
exec gosu agent env HOME=/tmp CONFIGS_DIR=/configs molecule-runtime
