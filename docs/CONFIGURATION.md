# Hermes workspace configuration

The active configuration is derived by `start.sh` from the workspace's
`/configs/config.yaml`, platform-injected resolution variables, and credential
environment. `config.yaml` in this repository defines the template defaults and
the model/provider choices shown by the platform.

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
