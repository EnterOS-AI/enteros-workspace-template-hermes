# Architecture — Molecule A2A bridge to Hermes

This document describes the code on the current branch. Historical rewrite
context lives in `PLANNING.md` and `MIGRATION.md` and is not an operating guide.

## Process and port map

```text
platform/canvas
      |
      v
molecule-runtime / A2A                    :8000
      |
      +--> Molecule MCP HTTP server       127.0.0.1:9100
      |
      v
HermesAgentProxyExecutor
      |
      +--> Hermes plugin transport        127.0.0.1:8645 (image default)
      |
      +--> chat-completions fallback      127.0.0.1:8642
                                                   ^
                                                   |
                                      hermes gateway
```

Only the A2A runtime is platform-facing. The Hermes and MCP surfaces are
loopback services inside the workspace. The image sets
`MOLECULE_A2A_PLATFORM_ENABLED=true`; explicitly disabling that flag selects
the chat-completions fallback on port 8642.

## Boot sequence

`start.sh` is the supported container entrypoint:

1. In smoke mode, skip gateway startup and execute `molecule-runtime` through
   the same unprivileged-user path used by the image checks.
2. Make `/configs` available to the uid-1000 `agent` process and load the
   platform-projected workspace configuration.
3. Create `/tmp/.hermes`, generate or reuse the loopback API key, and render
   the Hermes environment and config from the selected model/provider.
4. Start the Molecule MCP HTTP server on port 9100 and verify its JSON-RPC
   initialize response.
5. Start `hermes gateway` as `agent`, wait up to 120 seconds for port 8642's
   `/health`, and verify that Hermes sees the `molecule` MCP server.
6. Execute `molecule-runtime` as `agent` with `CONFIGS_DIR=/configs`.

Gateway and MCP logs live under `/tmp/.hermes` in the current image. Do not
document `/var/log` or `/home/agent/.hermes` as the active log/config path.

## Adapter setup

`HermesAgentAdapter.setup()`:

- builds the platform system prompt, including plugin rules/fragments;
- materializes the agent identity to Hermes' native persona file;
- installs declared plugins through the runtime's adapter registry;
- verifies the active Hermes transport health endpoint; and
- exposes adapter-native MCP configuration and loaded-tool enumeration.

Smoke mode intentionally skips live gateway/plugin probes.

## Turn execution

`HermesAgentProxyExecutor.execute()` extracts text and attachment metadata,
rejects a truly empty request with an actionable response, and preserves recent
canvas history.

- When `MOLECULE_A2A_PLATFORM_ENABLED` is true, the executor uses the Hermes
  plugin transport and waits for the callback reply.
- Otherwise it uses the loopback OpenAI-compatible chat-completions endpoint.

Both paths have bounded wall-clock timeouts. The executor also exposes the
Molecule MCP dispatcher so peer and workspace tools use the common runtime
contract instead of a template-local implementation.

## Sources of truth

| Concern | Source |
|---|---|
| Boot, rendered Hermes config, provider env | `start.sh` and `scripts/*.sh` |
| Adapter hooks and health selection | `adapter.py` |
| Transport choice and turn behavior | `executor.py` |
| Models/providers shown by the template | `config.yaml` |
| Image publication and pin checks | `.gitea/workflows/publish-image.yml` |

Open future architecture work as a Gitea issue. Do not append speculative
phases or manual deployment procedures to this file.
