#!/usr/bin/env bash
# derive-platform-llm.sh — when the workspace runs in platform-managed LLM
# billing, route hermes-agent inference through the Molecule platform proxy's
# OpenAI-compatible surface instead of any BYOK vendor path.
#
# Sourced by start.sh (and install.sh) AFTER scripts/derive-provider.sh, so it
# OVERRIDES the prefix-derived $PROVIDER when platform_managed is active. Kept
# as a separate sourceable script (mirrors derive-provider.sh) so Docker +
# bare-host entrypoints stay consistent and the routing is unit-testable
# without booting the whole container (tests/test_derive_platform_llm.sh).
#
# Contract:
#   Reads:  $MOLECULE_LLM_BILLING_MODE      "platform_managed" activates this
#           $MOLECULE_LLM_BASE_URL / $OPENAI_BASE_URL   platform OpenAI-compat base
#           $MOLECULE_LLM_USAGE_TOKEN / $ANTHROPIC_API_KEY   platform bearer token
#           $DEFAULT_MODEL                   selected model id (e.g. moonshot/kimi-k2.6)
#   Writes: $PROVIDER=custom and exports HERMES_CUSTOM_BASE_URL /
#           HERMES_CUSTOM_API_KEY / HERMES_CUSTOM_API_MODE=chat_completions,
#           and strips a leading "platform/" from $DEFAULT_MODEL.
#   On error (platform_managed set but no base URL): sets PLATFORM_LLM_ERROR=1
#           and returns/exits non-zero so the caller can fail closed rather
#           than silently fall back to a BYOK path with stripped keys.
#
# Why route EVERYTHING through hermes's `custom` provider: hermes-agent has no
# native multi-vendor "platform" provider. In platform-managed billing the
# tenant brings no key (the workspace-server strips BYOK provider keys), so a
# prefix-derived vendor path (kimi-coding, minimax, ...) would have no
# credential and fail. The platform proxy resolves the upstream vendor
# (moonshot/kimi, anthropic, minimax) from the model id and owns the keys +
# usage billing. This mirrors the proven smoke in
# molecule-controlplane/scripts/e2e-llm-kimi-smoke.sh: POST
# {base}/chat/completions with model "moonshot/kimi-k2.6" + a Bearer token.

if [ "${MOLECULE_LLM_BILLING_MODE:-}" != "platform_managed" ]; then
  # Not platform-managed — leave $PROVIDER as derive-provider.sh set it.
  return 0 2>/dev/null || exit 0
fi

_PLATFORM_BASE="${MOLECULE_LLM_BASE_URL:-${OPENAI_BASE_URL:-}}"
if [ -z "${_PLATFORM_BASE}" ]; then
  echo "[derive-platform-llm] platform_managed mode but neither MOLECULE_LLM_BASE_URL nor OPENAI_BASE_URL is set — cannot route LLM" >&2
  PLATFORM_LLM_ERROR=1
  return 1 2>/dev/null || exit 1
fi

PROVIDER="custom"
export HERMES_CUSTOM_BASE_URL="${_PLATFORM_BASE}"
export HERMES_CUSTOM_API_KEY="${MOLECULE_LLM_USAGE_TOKEN:-${ANTHROPIC_API_KEY:-}}"
# chat_completions (NOT codex_responses) — the platform proxy exposes
# /openai/v1/chat/completions, not /v1/responses.
export HERMES_CUSTOM_API_MODE="chat_completions"
# A "platform/" prefix is a canvas-only namespace marker; the proxy keys on
# the vendor prefix (moonshot/..., anthropic/..., minimax/...). Strip it if
# present so the upstream model id reaches the proxy unchanged.
DEFAULT_MODEL="${DEFAULT_MODEL#platform/}"
