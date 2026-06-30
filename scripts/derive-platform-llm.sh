#!/usr/bin/env bash
# derive-platform-llm.sh — when the workspace's resolved provider is `platform`,
# route hermes-agent inference through the Molecule platform proxy's
# OpenAI-compatible surface instead of any BYOK vendor path.
#
# Sourced by start.sh (and install.sh) AFTER scripts/derive-provider.sh, so it
# OVERRIDES the prefix-derived $PROVIDER when the resolved provider is platform.
# Kept as a separate sourceable script (mirrors derive-provider.sh) so Docker +
# bare-host entrypoints stay consistent and the routing is unit-testable
# without booting the whole container (tests/test_derive_platform_llm.sh).
#
# Selection is flag-free — `platform` is selected the same way any other
# provider is (provider==platform), NOT via a billing-mode env. The platform
# signal is any of:
#   $LLM_PROVIDER=platform            core injects this for platform-routed
#                                     workspaces (same signal the runtime
#                                     resolver + sibling adapters consume)
#   $HERMES_INFERENCE_PROVIDER=platform   explicit provider override == platform
#   $DEFAULT_MODEL == platform/*      a "platform/" model namespace marker
#
# Contract:
#   Reads:  the platform signals above (any one activates this)
#           $MOLECULE_LLM_BASE_URL / $OPENAI_BASE_URL   platform OpenAI-compat base
#           $MOLECULE_LLM_USAGE_TOKEN / $ANTHROPIC_API_KEY   platform bearer token
#           $DEFAULT_MODEL                   selected model id (e.g. moonshot/kimi-k2.6)
#   Writes: $PROVIDER=custom, exports MOLECULE_PLATFORM_LLM_ACTIVE=1 plus
#           HERMES_CUSTOM_BASE_URL / HERMES_CUSTOM_API_KEY /
#           HERMES_CUSTOM_API_MODE=chat_completions, and strips a leading
#           "platform/" from $DEFAULT_MODEL.
#   On error (provider==platform but no base URL): sets PLATFORM_LLM_ERROR=1
#           and returns/exits non-zero so the caller can fail closed rather
#           than silently fall back to a BYOK path with stripped keys.
#
# Why route EVERYTHING through hermes's `custom` provider: hermes-agent has no
# native multi-vendor "platform" provider. For the platform arm the tenant
# brings no key (the workspace-server strips BYOK provider keys), so a
# prefix-derived vendor path (kimi-coding, minimax, ...) would have no
# credential and fail. The platform proxy resolves the upstream vendor
# (moonshot/kimi, anthropic, minimax) from the model id and owns the keys +
# usage billing. This mirrors the proven smoke in
# molecule-controlplane/scripts/e2e-llm-kimi-smoke.sh: POST
# {base}/chat/completions with model "moonshot/kimi-k2.6" + a Bearer token.

_IS_PLATFORM=0
case "${LLM_PROVIDER:-}" in platform) _IS_PLATFORM=1 ;; esac
case "${HERMES_INFERENCE_PROVIDER:-}" in platform) _IS_PLATFORM=1 ;; esac
case "${DEFAULT_MODEL:-}" in platform/*) _IS_PLATFORM=1 ;; esac

if [ "${_IS_PLATFORM}" != "1" ]; then
  # Resolved provider is not platform — leave $PROVIDER as derive-provider.sh set it.
  return 0 2>/dev/null || exit 0
fi

_PLATFORM_BASE="${MOLECULE_LLM_BASE_URL:-${OPENAI_BASE_URL:-}}"
if [ -z "${_PLATFORM_BASE}" ]; then
  echo "[derive-platform-llm] resolved provider is platform but neither MOLECULE_LLM_BASE_URL nor OPENAI_BASE_URL is set — cannot route LLM" >&2
  PLATFORM_LLM_ERROR=1
  return 1 2>/dev/null || exit 1
fi

# Fail closed on a missing bearer too (symmetric with the base-URL check) —
# booting with an empty key would defer the failure to a runtime 401 at the
# proxy (offered-but-unservable) instead of a clear boot-time error.
_PLATFORM_TOKEN="${MOLECULE_LLM_USAGE_TOKEN:-${ANTHROPIC_API_KEY:-}}"
if [ -z "${_PLATFORM_TOKEN}" ]; then
  echo "[derive-platform-llm] resolved provider is platform but neither MOLECULE_LLM_USAGE_TOKEN nor ANTHROPIC_API_KEY is set — no platform bearer" >&2
  PLATFORM_LLM_ERROR=1
  return 1 2>/dev/null || exit 1
fi

# Mark platform routing active so downstream guards (the direct-base-URL
# refusal in start.sh / install.sh) key off provider==platform, not a
# billing-mode env.
export MOLECULE_PLATFORM_LLM_ACTIVE=1
PROVIDER="custom"
export HERMES_CUSTOM_BASE_URL="${_PLATFORM_BASE}"
export HERMES_CUSTOM_API_KEY="${_PLATFORM_TOKEN}"
# chat_completions (NOT codex_responses) — the platform proxy exposes
# /openai/v1/chat/completions, not /v1/responses.
export HERMES_CUSTOM_API_MODE="chat_completions"
# A "platform/" prefix is a canvas-only namespace marker; the proxy keys on
# the vendor prefix (moonshot/..., anthropic/..., minimax/...). Strip it if
# present so the upstream model id reaches the proxy unchanged.
DEFAULT_MODEL="${DEFAULT_MODEL#platform/}"
