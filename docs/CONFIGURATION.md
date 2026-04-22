# Configuration

Everything you can tune in a hermes workspace, where it lives, and how
the bridge sees it.

## Picking a model

**Via canvas Config tab** — the dropdown is populated from the `models:`
list in `config.yaml`. When you pick one, canvas writes the selection
into the workspace's runtime_config; molecule_runtime constructs
`AdapterConfig.model` from that; the bridge sends it verbatim as the
`model` field in the OpenAI-compat request payload. hermes-agent
resolves provider + auth from the string.

**Via `hermes` CLI** — open the workspace's Terminal tab and run
`hermes model`. This updates `~/.hermes/config.yaml` inside the
container and affects any subsequent A2A request.

**Which wins** — today the CLI and the bridge are independent.
If you set the model in the canvas AND in the CLI, each request
uses the one the bridge sends (the canvas value). An upcoming PR
will sync the two; see `ARCHITECTURE.md#future-work`.

## Provider keys

Set one or more of these as workspace-level secrets via
`POST /settings/secrets` (see monorepo `docs/runbooks/saas-secrets.md`).
All are forwarded into `~/.hermes/.env` at container boot.

| Env var              | Activates provider                                |
|----------------------|---------------------------------------------------|
| `HERMES_API_KEY`     | Nous Portal (Hermes 3, Hermes 4 direct)           |
| `OPENROUTER_API_KEY` | OpenRouter (200+ models)                          |
| `ANTHROPIC_API_KEY`  | Claude direct via Anthropic Messages API          |
| `OPENAI_API_KEY`     | GPT direct                                        |
| `GEMINI_API_KEY`     | Gemini direct via `google-genai`                  |
| `MINIMAX_API_KEY`    | MiniMax direct (sk-api-* or sk-cp-* accepted)     |

You don't pick the provider yourself. hermes-agent resolves it from
the `model` string prefix — `anthropic/` → Anthropic, `gemini/` →
Gemini, `nousresearch/` → Nous Portal (if `HERMES_API_KEY` present)
falling back to OpenRouter, etc.

## Persisting skills + memory

`hermes-agent` stores everything stateful under `~/.hermes`:

```
/home/agent/.hermes/
├── .env               ← provider keys + API_SERVER_* (regenerated per boot)
├── config.yaml        ← model, tools, gateway settings
├── skills/            ← self-improvement loop writes here
├── sessions/          ← conversation history (FTS5-indexed)
├── memory/            ← long-lived user model (Honcho + custom)
└── logs/
```

For these to survive a workspace container restart, the platform
needs a Docker volume mounted at `/home/agent/.hermes`. The default
provisioner config already handles this — verify with:

```bash
docker inspect --format='{{json .Mounts}}' <workspace-container-id>
```

If `/home/agent/.hermes` is not in the Mounts list, edit the
workspace's provisioner config in the monorepo.

## Gateway platforms (advanced)

`hermes-agent` ships with Telegram, Discord, Slack, WhatsApp, Signal,
and ~10 other platform adapters. v2.0.0 of this template wires only
the `api_server` platform (required for the A2A bridge).

To enable another platform, customize `~/.hermes/.env` in the workspace:

```bash
docker exec -it -u agent <workspace-container> bash -lc '
  cat >> ~/.hermes/.env <<EOF
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=...
EOF
  # Restart the gateway process to pick up changes:
  pkill -f "hermes gateway" || true
  nohup hermes gateway >/var/log/hermes-gateway.log 2>&1 &
'
```

This is not yet surfaced in canvas. Follow the issue tracker for
first-class gateway-platform support.

## Restarting the gateway

If `hermes gateway` crashes (symptom: every request returns
`[hermes-agent unreachable]`), restart it without restarting the
whole workspace:

```bash
docker exec -u agent <workspace-container> bash -lc '
  pkill -f "hermes gateway" || true
  nohup hermes gateway >/var/log/hermes-gateway.log 2>&1 &
'
```

molecule_runtime on :8000 is unaffected.

## Inspecting state

```bash
# What model is the CLI pinned to?
docker exec -u agent <id> hermes model

# What tools are enabled?
docker exec -u agent <id> hermes tools

# How is the agent doing?
docker exec -u agent <id> hermes doctor

# Last 200 lines of gateway log:
docker exec <id> tail -200 /var/log/hermes-gateway.log
```

## Bridge timeouts

`executor.py` uses a 600-second httpx timeout. If you run agent
turns that take longer than 10 minutes (large research tasks with
many tool calls), bump `_REQUEST_TIMEOUT` in `executor.py` and rebuild
the image. Don't try to configure this at runtime via env — we keep
it in code so regressions are version-controlled.
