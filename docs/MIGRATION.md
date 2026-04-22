# Migration — v1.x → v2.0.0

## TL;DR

There's nothing to migrate at the customer level. v1.x of this
template was only used by the staging canary (`E2E_RUNTIME=hermes`)
and had no production workspaces. v2.0.0 replaces the template image
in place; next provision uses the new Dockerfile.

## What went away

The following files were removed because their responsibilities are
now owned by the real hermes-agent inside the container:

- `providers.py` — 15-provider registry, routing table, env-var lookup.
  hermes-agent does this via `hermes model` (CLI) and its own
  provider resolver; see `hermes_cli/codex_models.py` upstream.
- `escalation.py` — retry + fallback ladder across providers.
  hermes-agent's skill-and-memory loop supersedes this pattern.
- `requirements.txt` entries for `openai`, `anthropic`, `google-genai`.
  hermes-agent pulls these itself as needed.

## Behavioural changes customers will see

1. **Model selection is broader.** Instead of the 6-provider list in
   v1.x, you can run any model hermes-agent supports — Nous Portal,
   OpenRouter (200+ models), NVIDIA NIM, Xiaomi MiMo, z.ai/GLM, Kimi,
   MiniMax, Hugging Face, OpenAI, Anthropic direct, Gemini direct,
   xAI, Together, Fireworks, Mistral, or your own endpoint.
2. **Agent has tools now.** The workspace can use terminal, files,
   web search, memory, and skills — all real tools executed inside
   the container. v1.x responses were text-only chat completions.
3. **Skills persist.** Nous Research's self-improvement loop writes
   skills into `~/.hermes/skills/` on disk. Volume-mount
   `/home/agent/.hermes` to keep them across restarts.
4. **Provider routing is invisible.** You set one or more provider
   keys as workspace secrets; hermes-agent picks the right one per
   model string. v1.x required you to understand the `provider`
   field; v2.0.0 does not have that field at all.

## Config.yaml diff (relevant part)

```diff
- runtime_config:
-   model: nous-hermes-3-70b
-   models:
-     - id: nous-hermes-3-70b
-       ...
-   required_env:
-     - HERMES_API_KEY
+ runtime_config:
+   model: nousresearch/hermes-4-70b
+   models:
+     - id: nousresearch/hermes-4-70b
+       ...
+   required_env:
+     - HERMES_API_KEY
+ bridge:
+   hermes_api_base: http://127.0.0.1:8642/v1
+   hermes_api_key_env: API_SERVER_KEY
```

If you have a custom workspace config.yaml that extended v1.x, drop:

- `runtime_config.provider` — no longer honored (hermes-agent picks
  provider from the `model` string).
- `runtime_config.escalation_ladder` — remove; hermes-agent's
  skill/memory loop replaces this pattern.
- Any `providers.py`-specific IDs — translate to hermes-agent's
  canonical form (`openrouter/<slug>`, `anthropic/<id>`, etc.).

## Env var changes

No removals. `HERMES_API_KEY`, `OPENROUTER_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, and
`MINIMAX_API_KEY` are all still honored — they're forwarded into
`~/.hermes/.env` at container boot.

New internal vars (not customer-facing, documented for operators):

| Var                 | Purpose                                                   |
|---------------------|-----------------------------------------------------------|
| `HERMES_API_BASE`   | Where the bridge sends requests (default `http://127.0.0.1:8642/v1`) |
| `API_SERVER_KEY`    | Bearer for the loopback API. Generated per boot.          |
| `API_SERVER_HOST`   | Where hermes gateway binds its API server (default `127.0.0.1`) |
| `API_SERVER_PORT`   | Port hermes gateway listens on (default `8642`)           |

## CI

Staging canary workflow (`.github/workflows/canary-staging.yml` in
molecule-monorepo) and e2e suites (`.github/workflows/e2e-staging-saas.yml`)
should both pass against v2.0.0 unchanged. The runtime name (`hermes`)
didn't change; only what's behind it did.

## Rollback

If v2.0.0 fails in production, pinning the canvas `workspace-template:hermes`
back to the pre-rewrite digest is a one-line change in the monorepo
provisioner. v1.x files are still recoverable from this repo's git
history (`git show <pre-rewrite-sha>:adapter.py`, etc.).
