#!/usr/bin/env bash
# derive-provider.sh — map a hermes-agent model slug to its provider
# name. Sourced by both install.sh (SaaS bare-host path) and start.sh
# (Docker path) so the two entry-points stay consistent.
#
# Contract:
#   Reads:  $HERMES_INFERENCE_PROVIDER (if already set, we respect it)
#           $HERMES_DEFAULT_MODEL      (slug, e.g. "minimax/MiniMax-M2.7-highspeed")
#           $HERMES_API_KEY / $NOUS_API_KEY (affect the nousresearch/* branch)
#   Writes: $PROVIDER — the derived provider name, or "auto" if unknown.
#
# Why the per-template sub-script (vs doing this in CP): every runtime
# has its own provider taxonomy. Keeping the logic inside the template
# repo means CP stays runtime-agnostic and adding a new runtime with
# different provider semantics doesn't require a CP edit.
#
# Hermes-specific quirks encoded here:
#   - `openai/...` routes through `openrouter` (hermes has no direct
#     openai provider; openai-codex is OAuth-only for Codex models)
#   - `nousresearch/...` prefers direct `nous` if HERMES_API_KEY is
#     set, else falls back to `openrouter` (which also serves Hermes 3)
#   - chinese-region variants (minimax-cn, kimi-coding-cn) keep their
#     full prefix as the provider name
#
# See molecule-controlplane/docs/canary-tenants.md and the hermes-agent
# providers.md docs for the full taxonomy.

# Honour an explicit override.
if [ -n "${HERMES_INFERENCE_PROVIDER:-}" ]; then
  PROVIDER="${HERMES_INFERENCE_PROVIDER}"
  return 0 2>/dev/null || exit 0
fi

if [ -z "${HERMES_DEFAULT_MODEL:-}" ]; then
  PROVIDER="auto"
  return 0 2>/dev/null || exit 0
fi

case "${HERMES_DEFAULT_MODEL}" in
  # Keep full CN-suffix as provider so chinese-region keys route right
  minimax-cn/*)            PROVIDER="minimax-cn" ;;
  kimi-coding-cn/*)        PROVIDER="kimi-coding-cn" ;;

  # Direct-SDK providers (clean 1:1 prefix→provider mapping)
  minimax/*)               PROVIDER="minimax" ;;
  anthropic/*)             PROVIDER="anthropic" ;;
  gemini/*)                PROVIDER="gemini" ;;
  deepseek/*)              PROVIDER="deepseek" ;;
  zai/*)                   PROVIDER="zai" ;;
  kimi-coding/*)           PROVIDER="kimi-coding" ;;
  alibaba/*|dashscope/*|qwen/*) PROVIDER="alibaba" ;;
  xiaomi/*|mimo/*)         PROVIDER="xiaomi" ;;
  arcee/*|arcee-ai/*)      PROVIDER="arcee" ;;
  nvidia/*|nim/*)          PROVIDER="nvidia" ;;
  ollama-cloud/*)          PROVIDER="ollama-cloud" ;;
  huggingface/*|hf/*)      PROVIDER="huggingface" ;;
  ai-gateway/*|aigateway/*) PROVIDER="ai-gateway" ;;
  kilocode/*)              PROVIDER="kilocode" ;;
  opencode-zen/*)          PROVIDER="opencode-zen" ;;
  opencode-go/*)           PROVIDER="opencode-go" ;;

  # Hermes-specific routing quirks
  openai/*)                PROVIDER="openrouter" ;;  # no direct openai provider; openrouter covers it
  nousresearch/*)
    # Prefer direct Nous Portal if Nous credentials present, else OR.
    if [ -n "${HERMES_API_KEY:-}" ] || [ -n "${NOUS_API_KEY:-}" ]; then
      PROVIDER="nous"
    else
      PROVIDER="openrouter"
    fi
    ;;

  # Explicit catch-alls
  openrouter/*)            PROVIDER="openrouter" ;;
  custom/*)                PROVIDER="custom" ;;

  # Unknown prefix → let hermes auto-detect
  *)                       PROVIDER="auto" ;;
esac
