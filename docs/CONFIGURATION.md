# Hermes workspace configuration

The active configuration is derived by `start.sh` from the workspace's
`/configs/config.yaml`, platform-injected resolution variables, and credential
environment. `config.yaml` in this repository defines the template defaults and
the model/provider choices shown by the platform.

The template defaults to platform-managed MiniMax M3 (`minimax/MiniMax-M3`),
which uses the platform proxy and needs no personal MiniMax key. The separate
`minimax:MiniMax-M3` choice uses `MINIMAX_API_KEY`. Existing explicit model
selections keep their normal precedence.

## Model and provider precedence

1. Platform-resolved model/provider values are authoritative when present.
2. Explicit `HERMES_INFERENCE_MODEL` and `HERMES_INFERENCE_PROVIDER` values are
   honored by the boot helpers.
3. Otherwise the helper derives the provider from the selected model and
   available supported credential.

The rendered Hermes config is written under `/tmp/.hermes` in the current
container. Editing a different home-directory config does not change the booted
gateway.

## Credential variables forwarded by `start.sh`

Provide credentials through the workspace secret configuration. Never put a
real value in this repository or a shell transcript.

| Provider family | Accepted variables |
|---|---|
| Nous | `NOUS_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Gemini | `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| GLM | `GLM_API_KEY` |
| Kimi | `KIMI_API_KEY`, `KIMI_CN_API_KEY` |
| MiniMax | `MINIMAX_API_KEY`, `MINIMAX_CN_API_KEY` |
| Alibaba/Qwen | `DASHSCOPE_API_KEY` |
| Xiaomi | `XIAOMI_API_KEY` |
| Arcee | `ARCEEAI_API_KEY` |
| NVIDIA | `NVIDIA_API_KEY` |
| Ollama Cloud | `OLLAMA_API_KEY` |
| Hugging Face | `HF_TOKEN` |
| AI gateway | `AI_GATEWAY_API_KEY` |
| Kilo Code | `KILOCODE_API_KEY` |
| OpenCode | `OPENCODE_ZEN_API_KEY`, `OPENCODE_GO_API_KEY` |
| Copilot | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` |

`HERMES_API_KEY` is not forwarded as a primary inference credential by the
current boot script; use the provider-specific variable declared above and in
`config.yaml`.

## Platform-managed inference

For a platform-resolved route, the boot helpers translate the injected model,
base URL, and usage credential into the Hermes custom-provider shape. Do not
override that route with an unrelated direct-provider endpoint. The shell tests
`test-derive-platform-llm.sh` and `test-default-model-selection.sh` guard this
behavior.

## Molecule tools and plugins

The boot path writes the native Hermes MCP descriptor for the loopback
`molecule` server. `adapter.py` installs declared plugins through the runtime's
adapter registry and materializes compatible skills/rules before serving. Do
not add a second shell plugin installer.

## Session, memory, and skill-review knobs

Set these as workspace environment variables or secrets. Each is optional and
unset keeps current behaviour. An invalid value is logged at WARNING during
boot and ignored.

| Variable | Effect | Accepted values |
|---|---|---|
| `HERMES_SESSION_RESET_MODE` | Start a fresh Hermes session (Hermes' own `/new`) before the first turn past a boundary | `none` (default), `idle`, `daily`, `both` |
| `HERMES_SESSION_RESET_HOUR` | Daily boundary, container local time (UTC in the platform image) | `0`-`23`; default `4` |
| `HERMES_SESSION_IDLE_MINUTES` | Idle gap since the previous turn finished | integer `>= 1`; default `1440` |
| `HERMES_MEMORY_CHAR_LIMIT` | `memory.memory_char_limit` | integer `>= 1`; Hermes default `2200` |
| `HERMES_USER_CHAR_LIMIT` | `memory.user_char_limit` | integer `>= 1`; Hermes default `1375` |
| `HERMES_MEMORY_NUDGE_INTERVAL` | `memory.nudge_interval`, user turns between background memory reviews | integer `>= 0`, `0` turns the review off; Hermes default `10` |
| `HERMES_SKILL_NUDGE_INTERVAL` | `skills.creation_nudge_interval`, tool iterations between background skill reviews | integer `>= 0`, `0` turns the review off; Hermes default `10` |

`start.sh` writes the memory and skill keys into the generated
`/tmp/.hermes/config.yaml`, which is the file the gateway reads. They apply to
agents created after boot. A hand edit of that file is lost on restart because
`start.sh` regenerates it.

The session-reset knobs are applied by `executor.py`, not by Hermes. The pinned
Hermes ignores `session_reset:` in `config.yaml` (see `known-issues.md`). A
reset is never sent while another turn on the same chat is running, and the
first turn after a container start only starts the clock. They apply to the
plugin transport, which is the image default.

## Bundled Node.js

Hermes' package manager installs Node.js and npm into its tool store
(`~/.hermes/tools`); `scripts/hermes-post-install.sh` links their `node`,
`npm`, and `npx` into `/home/agent/.hermes/node/bin` at build time. When `node`
and `npx` are present there, `start.sh` prepends that directory to the
gateway's `PATH`. The image also
installs the same rule as `/etc/profile.d/hermes-node-path.sh`. The terminal
tool runs commands from a login-shell snapshot, where Debian's `/etc/profile`
replaces `PATH`, so this file is what puts Node.js on the terminal's `PATH`.

## State and continuity

The current Hermes home is `/tmp/.hermes`, so gateway-local state is not the
durable source of conversation continuity. `executor.py` forwards recent
platform history to seed a fresh Hermes session. Do not claim that a default
mount under `/home/agent/.hermes` preserves this image's live session store.

## Diagnostics

- Gateway log: `/tmp/.hermes/gateway.log`
- Molecule MCP log: `/tmp/.hermes/molecule-mcp-server.log`
- Gateway health: `http://127.0.0.1:8642/health`
- Molecule MCP endpoint: `http://127.0.0.1:9100/mcp`

These are container-internal diagnostics, not public platform endpoints.
