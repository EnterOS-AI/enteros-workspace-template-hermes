#!/usr/bin/env bash
# derive-minimax-bridge.sh — route a DIRECT (BYOK) MiniMax key through the
# hermes `custom` provider in OpenAI-compat mode.
#
# Sourced by BOTH install.sh (bare-host path) and start.sh (Docker path) — the
# same SSOT discipline as derive-provider.sh / derive-platform-llm.sh, so the two
# entry-points cannot drift. (The pre-existing OpenAI bridge is still inlined
# separately in each file and is already subtly different between them; it
# SHOULD migrate here too. That drift is exactly what this file exists to
# prevent.)
#
# WHY THIS EXISTS
#   hermes's NATIVE `minimax` provider drives the ANTHROPIC request shape
#   (POST <base>/v1/messages). api.minimax.io does not serve that path. Measured
#   against the live endpoint:
#
#       POST https://api.minimax.io/v1/messages          -> 404 page not found
#       POST https://api.minimax.io/v1/chat/completions  -> 401 (exists, wants auth)
#
#   so a BYOK MiniMax workspace 404s on EVERY turn: the canvas shows "The model
#   provider failed after retries" and the gateway log carries
#   anthropic.NotFoundError. The SaaS path never hit this because the control
#   plane routes MiniMax as custom+chat_completions through its OpenAI-compat
#   proxy; only the DIRECT BYOK path reaches the native provider.
#
# CONTRACT
#   Reads:  $PROVIDER (minimax | minimax-cn), $MINIMAX_API_KEY /
#           $MINIMAX_CN_API_KEY, $MINIMAX_BASE_URL (override),
#           $MOLECULE_PLATFORM_LLM_ACTIVE, $HERMES_CUSTOM_* (operator override),
#           $DEFAULT_MODEL
#   Writes: $PROVIDER, $HERMES_CUSTOM_BASE_URL, $HERMES_CUSTOM_API_KEY,
#           $HERMES_CUSTOM_API_MODE, $DEFAULT_MODEL
#   No-ops (leaves everything untouched) when:
#     - PROVIDER is not a MiniMax variant
#     - no MiniMax key is present
#     - the operator already configured HERMES_CUSTOM_BASE_URL/API_KEY
#     - platform LLM routing is active (derive-platform-llm.sh owns routing
#       there, and the caller's guard refuses a non-proxy base_url in that mode)
#
# BASE URLs are duplicated from the `providers:` block in config.yaml, which in
# turn mirrors derive-provider.sh's case arms and the control plane's
# KnownProviderNames(). That 4-way sync burden is a known wart: the provider
# registry is SDK-owned SSOT, so these endpoints should come from the vendored
# provider contract rather than being spelled again here. Keep in step with
# config.yaml until that lands.

molecule_derive_minimax_bridge() {
    # Platform-managed routing owns the base_url; never override it.
    [ "${MOLECULE_PLATFORM_LLM_ACTIVE:-}" = "1" ] && return 0
    [ "${MOLECULE_LLM_BILLING_MODE:-}" = "platform_managed" ] && return 0

    _mm_key=""
    _mm_base=""
    case "${PROVIDER:-}" in
        minimax-cn)
            _mm_key="${MINIMAX_CN_API_KEY:-${MINIMAX_API_KEY:-}}"
            _mm_base="https://api.minimaxi.com/v1"
            ;;
        minimax)
            _mm_key="${MINIMAX_API_KEY:-}"
            _mm_base="https://api.minimax.io/v1"
            ;;
        *)
            return 0
            ;;
    esac

    # Operator-supplied custom routing always wins (vLLM / LM Studio / a private
    # OpenAI-compat gateway) — same precedence as the OpenAI bridge.
    if [ -z "${_mm_key}" ] || [ -n "${HERMES_CUSTOM_BASE_URL:-}" ] || [ -n "${HERMES_CUSTOM_API_KEY:-}" ]; then
        unset _mm_key _mm_base
        return 0
    fi

    export HERMES_CUSTOM_BASE_URL="${MINIMAX_BASE_URL:-${_mm_base}}"
    export HERMES_CUSTOM_API_KEY="${_mm_key}"
    export HERMES_CUSTOM_API_MODE="chat_completions"
    # MiniMax expects the BARE model id (MiniMax-M2.7-highspeed) once requests
    # land on its OpenAI-compat surface, not the prefixed slug.
    DEFAULT_MODEL="${DEFAULT_MODEL#minimax-cn/}"
    DEFAULT_MODEL="${DEFAULT_MODEL#minimax/}"
    PROVIDER="custom"

    # NEVER print the key.
    echo "[${MOLECULE_BRIDGE_CALLER:-derive-minimax-bridge}] bridged MiniMax key -> custom provider @ ${HERMES_CUSTOM_BASE_URL} (api_mode=chat_completions, model=${DEFAULT_MODEL})"
    unset _mm_key _mm_base
}

molecule_derive_minimax_bridge
