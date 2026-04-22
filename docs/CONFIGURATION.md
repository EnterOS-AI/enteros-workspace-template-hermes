# Configuration

Everything you can tune in a hermes workspace, where it lives, and how
the bridge sees it.

## Picking a model

**Via canvas Config tab** — the dropdown is populated from the `models:`
list in `config.yaml`. When you pick one, canvas writes the selection
into the workspace's runtime_config; molecule_runtime constructs
`AdapterConfig.model` from that; the bridge sends it verbatim as the
`model` field in the OpenAI-compat request payload. hermes-agent
resolves provider + auth from the string (see provider matrix below).

**Via `hermes` CLI** — open the workspace's Terminal tab and run
`hermes model`. This updates `~/.hermes/cli-config.yaml` inside the
container and affects any subsequent A2A request.

**Which wins** — today the CLI and the bridge are independent.
If you set the model in the canvas AND in the CLI, each A2A request
uses the one the bridge sends (the canvas value). An upcoming PR
will sync the two; see `ARCHITECTURE.md#future-work`.

## Provider matrix

hermes-agent supports every provider below. Set the corresponding env
var as a workspace secret (`POST /settings/secrets` — see monorepo
`docs/runbooks/saas-secrets.md`). start.sh forwards it into
`~/.hermes/.env` at container boot.

### OAuth-based providers

These require `hermes model` to be run interactively (Terminal tab,
non-piped). Set up once; tokens stored at `~/.hermes/auth/`.

| Provider                | How to set up                                                                   |
|-------------------------|---------------------------------------------------------------------------------|
| **Nous Portal**         | `hermes model` → Nous Portal OAuth (subscription)                               |
| **OpenAI Codex**        | `hermes model` → ChatGPT OAuth (uses GPT-5-Codex family)                        |
| **GitHub Copilot**      | `hermes model` → OAuth device code, or set `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` |
| **Anthropic (Claude Pro/Max)** | `hermes model` → Claude Code auth, or set `ANTHROPIC_API_KEY` for API-key mode |
| **Google Gemini OAuth** | `hermes model` → "Google Gemini (OAuth)". Free tier, PKCE. See provider-routing docs for GCP-project caveats |

### API-key providers

Just set the env var; hermes-agent picks up the key at boot.

| Provider           | Env var                | Example model IDs                                      |
|--------------------|------------------------|-------------------------------------------------------|
| **Nous Portal API**| `HERMES_API_KEY` (or `NOUS_API_KEY`)  | `nousresearch/hermes-4-70b`, `nousresearch/hermes-4-405b`, `nousresearch/hermes-4-14b` |
| **OpenRouter**     | `OPENROUTER_API_KEY`   | Anything on openrouter.ai (`openai/gpt-5`, `anthropic/claude-sonnet-4-5`, 200+ others) |
| **OpenAI (via OpenRouter)** | `OPENAI_API_KEY` alt-auth on openrouter | `openai/gpt-5`, `openai/gpt-4o`, `openai/gpt-4o-mini` |
| **Anthropic**      | `ANTHROPIC_API_KEY`    | `anthropic/claude-sonnet-4-5`, `anthropic/claude-opus-4-1`, `anthropic/claude-haiku-4-5` |
| **Google Gemini**  | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `gemini/gemini-2.5-pro`, `gemini/gemini-2.5-flash` |
| **DeepSeek**       | `DEEPSEEK_API_KEY`     | `deepseek/deepseek-v3.2`, `deepseek/deepseek-r1`        |
| **z.ai / GLM**     | `GLM_API_KEY`          | `zai/glm-4.6`                                           |
| **Kimi / Moonshot**| `KIMI_API_KEY` (global), `KIMI_CN_API_KEY` (China) | `kimi-coding/kimi-k2` |
| **MiniMax**        | `MINIMAX_API_KEY` (global), `MINIMAX_CN_API_KEY` (China) | `minimax/MiniMax-M2.7`, `minimax-cn/abab6.5-chat` |
| **Alibaba / Qwen** | `DASHSCOPE_API_KEY`    | `alibaba/qwen3-max`, `alibaba/qwen3-coder`              |
| **Xiaomi MiMo**    | `XIAOMI_API_KEY`       | `xiaomi/mimo-v1`                                        |
| **Arcee Trinity**  | `ARCEEAI_API_KEY`      | `arcee/trinity-70b`                                     |
| **NVIDIA NIM**     | `NVIDIA_API_KEY`       | `nvidia/nemotron-70b`                                   |
| **Ollama Cloud**   | `OLLAMA_API_KEY`       | `ollama-cloud/llama-3.3-70b`                            |
| **Hugging Face**   | `HF_TOKEN`             | `huggingface/*` (any HF inference model)                |
| **Vercel AI Gateway** | `AI_GATEWAY_API_KEY` | `ai-gateway/*`                                          |
| **Kilo Code**      | `KILOCODE_API_KEY`     | `kilocode/*`                                            |
| **OpenCode Zen**   | `OPENCODE_ZEN_API_KEY` | `opencode-zen/*`                                        |
| **OpenCode Go**    | `OPENCODE_GO_API_KEY`  | `opencode-go/*`                                         |

### Self-hosted / local

`hermes model` → "Custom endpoint" — any OpenAI-compatible HTTP API.
Aliases for quick setup: `lmstudio`, `ollama`, `vllm`, `llamacpp`.

```yaml
# example ~/.hermes/cli-config.yaml override
model:
  default: "llama-3.3-70b-instruct"
  provider: "lmstudio"
  base_url: "http://host.docker.internal:1234/v1"
```

No API key needed — local servers typically ignore auth.

## Forcing a provider

By default hermes-agent's provider-selection is `auto` — it walks its
internal resolution order and picks the first available credential.
This can route surprising ways when multiple keys are set (e.g. an
`OPENAI_API_KEY` will fall to `openai-codex` which is OAuth-only and
returns 401 on API-key auth).

To force a specific provider, set `HERMES_INFERENCE_PROVIDER` on the
workspace container. start.sh writes it into `~/.hermes/cli-config.yaml`
and `~/.hermes/.env` at boot. Valid values (from hermes-agent
`cli-config.yaml.example`):

```
auto | openrouter | nous | nous-api | anthropic | openai-codex
copilot | gemini | google-gemini-cli | zai | kimi-coding | kimi-coding-cn
minimax | minimax-cn | alibaba (aliases: dashscope, qwen)
arcee | nvidia | xiaomi | huggingface | ollama-cloud
ai-gateway | kilocode | opencode-zen | opencode-go | deepseek | custom
```

**Most common choices when multiple keys are present:**
- `OPENAI_API_KEY` only → `HERMES_INFERENCE_PROVIDER=openrouter` (hermes
  openrouter accepts OPENAI_API_KEY as alt auth)
- `ANTHROPIC_API_KEY` only → `anthropic`
- Mixed keys → `auto` usually works

## Auxiliary model (vision / MoA / summarization)

hermes-agent uses a second, smaller model for vision, web page
summarization, and mixture-of-agents tool calls. Defaults to
**Gemini Flash via OpenRouter**. Having `OPENROUTER_API_KEY` set is
enough; otherwise vision + web-summarize + MoA break silently.

Override the auxiliary path with `HERMES_AUXILIARY_PROVIDER` env —
start.sh forwards it. See hermes-agent
[Auxiliary Models docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/configuration.md)
for the full field set.

## Persisting skills + memory

`hermes-agent` stores everything stateful under `~/.hermes`:

```
/home/agent/.hermes/
├── .env                 ← provider keys + API_SERVER_* (regenerated per boot)
├── cli-config.yaml      ← model + provider selection (seeded by start.sh if absent)
├── hermes-agent/        ← the installed project; venv, source, upstream repo
├── auth/                ← OAuth tokens (Google Gemini OAuth, Copilot, Codex, etc.)
├── skills/              ← self-improvement loop writes here
├── sessions/            ← conversation history (FTS5-indexed)
├── memory/              ← long-lived user model (Honcho + custom)
└── logs/
```

For these to survive a container restart, mount a Docker volume at
`/home/agent/.hermes`. The platform's default provisioner config does
this already — verify with:

```bash
docker inspect --format='{{json .Mounts}}' <workspace-container-id>
```

## Gateway platforms (advanced)

`hermes-agent` ships with Telegram, Discord, Slack, WhatsApp, Signal,
and ~10 other platform adapters. v2.x of this template wires only the
`api_server` platform (required for the A2A bridge).

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

Not yet surfaced in canvas. Follow the issue tracker for first-class
gateway-platform support.

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
docker exec -u agent <id> hermes model show

# What tools are enabled?
docker exec -u agent <id> hermes tools

# Doctor report (warnings, missing deps, broken providers):
docker exec -u agent <id> hermes doctor

# Last 200 lines of gateway log:
docker exec <id> tail -200 /var/log/hermes-gateway.log
```

## Bridge timeouts

`executor.py` uses a 600-second httpx timeout. If you run agent turns
that take longer than 10 minutes (large research tasks with many tool
calls), bump `_REQUEST_TIMEOUT` in `executor.py` and rebuild the
image. Don't try to configure this at runtime via env — we keep it in
code so regressions are version-controlled.
