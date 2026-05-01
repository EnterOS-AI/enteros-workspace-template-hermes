#!/usr/bin/env bash
# load-workspace-config.sh — bridge the workspace-level /configs/config.yaml
# (written by molecule-controlplane user-data per task #197) into the
# hermes-specific HERMES_DEFAULT_MODEL / HERMES_INFERENCE_PROVIDER env
# vars that start.sh + derive-provider.sh consume.
#
# Why this exists: PR-3 in the Option B series taught CP to write
# `runtime_config.model` and `runtime_config.provider` into
# /configs/config.yaml at provision time so the canvas Config tab can
# round-trip the operator's pick. start.sh used to only read the
# HERMES_* env vars, which CP doesn't set, so the config.yaml fields
# were silently ignored — every workspace booted with the built-in
# `nousresearch/hermes-4-70b` default and 500'd at first prompt with
# "No LLM provider configured" (visible in the 2026-04-30 hongmingwang
# tenant screenshots).
#
# Precedence (highest to lowest):
#   1. HERMES_DEFAULT_MODEL / HERMES_INFERENCE_PROVIDER env vars
#      (operator override via workspace secrets — they win)
#   2. /configs/config.yaml runtime_config.{model,provider}
#      (canvas Config tab — set via UI, written by CP user-data)
#   3. start.sh's hard-coded fallback (nousresearch/hermes-4-70b)
#
# Contract:
#   Reads:   /configs/config.yaml (or $MOLECULE_CONFIG_PATH/config.yaml)
#            $HERMES_DEFAULT_MODEL, $HERMES_INFERENCE_PROVIDER
#   Writes:  HERMES_DEFAULT_MODEL (only if unset and config.yaml has it)
#            HERMES_INFERENCE_PROVIDER (only if unset and config.yaml has it)
#
# Failure modes (silent — never blocks boot):
#   - /configs/config.yaml doesn't exist → no-op
#   - python3 not on PATH → no-op (start.sh's fallback still works)
#   - PyYAML not importable → no-op
#   - Malformed YAML → no-op
#   - runtime_config absent or not a dict → no-op
#
# Resilience over completeness — same philosophy as the claude-code
# adapter's _load_providers fallback. A workspace with a missing or
# malformed config.yaml should still boot and fall through to the
# env-var/built-in defaults instead of dying at this step.

# Source-only safety: don't `set -e` here — this script is `.`-sourced
# by start.sh which already has its own set -euo pipefail. Errors here
# would otherwise kill the parent shell.

_lwc_config_path="${MOLECULE_CONFIG_PATH:-/configs}/config.yaml"

# Skip silently if the file isn't there. Workspaces booted before PR-3
# rolled out, or non-CP-provisioned dev containers, won't have it.
if [ ! -f "$_lwc_config_path" ]; then
  unset _lwc_config_path
  return 0 2>/dev/null || true
fi

# Skip if python3 is missing — start.sh's existing logic still works.
if ! command -v python3 >/dev/null 2>&1; then
  unset _lwc_config_path
  return 0 2>/dev/null || true
fi

# Single python invocation extracts both fields and prints `key=value`
# lines. Importing yaml inside try/except so a runtime missing PyYAML
# (shouldn't happen in production — molecule-ai-workspace-runtime
# brings it transitively — but defensive against dev images) yields
# zero output instead of an error. Any exception → empty output → the
# read loop below sets nothing and start.sh keeps its fallbacks.
_lwc_extracted=$(MOLECULE_CONFIG_FILE="$_lwc_config_path" python3 - <<'PYEOF' 2>/dev/null
import os, sys
try:
    import yaml
except ImportError:
    sys.exit(0)
try:
    with open(os.environ["MOLECULE_CONFIG_FILE"]) as f:
        data = yaml.safe_load(f) or {}
except Exception:
    sys.exit(0)
rc = data.get("runtime_config") or {}
if not isinstance(rc, dict):
    sys.exit(0)
# Print one key=value per line; empty values omitted so the bash side
# can use [ -n "$value" ] without ambiguity. Values are stringified so
# non-string YAML scalars (e.g. integers) don't break the read loop.
for env_name, yaml_key in (
    ("HERMES_DEFAULT_MODEL", "model"),
    ("HERMES_INFERENCE_PROVIDER", "provider"),
):
    v = rc.get(yaml_key)
    if v is None:
        continue
    s = str(v).strip()
    if s:
        print(f"{env_name}={s}")
PYEOF
)

# Apply the extracted values, but only when the corresponding env var
# isn't already set — operator override (env-var pre-set, e.g. via
# workspace secrets) must beat the YAML.
while IFS='=' read -r _lwc_key _lwc_value; do
  [ -z "$_lwc_key" ] && continue
  case "$_lwc_key" in
    HERMES_DEFAULT_MODEL)
      if [ -z "${HERMES_DEFAULT_MODEL:-}" ] && [ -n "$_lwc_value" ]; then
        export HERMES_DEFAULT_MODEL="$_lwc_value"
        echo "[load-workspace-config] HERMES_DEFAULT_MODEL=$_lwc_value (from $_lwc_config_path)" >&2
      fi
      ;;
    HERMES_INFERENCE_PROVIDER)
      if [ -z "${HERMES_INFERENCE_PROVIDER:-}" ] && [ -n "$_lwc_value" ]; then
        export HERMES_INFERENCE_PROVIDER="$_lwc_value"
        echo "[load-workspace-config] HERMES_INFERENCE_PROVIDER=$_lwc_value (from $_lwc_config_path)" >&2
      fi
      ;;
  esac
done <<<"$_lwc_extracted"

unset _lwc_config_path _lwc_extracted _lwc_key _lwc_value
