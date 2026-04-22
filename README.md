# template-hermes

Molecule AI workspace template that runs the **real Nous Research
[hermes-agent](https://github.com/NousResearch/hermes-agent)** behind an
A2A bridge.

## What's actually in the container

- **hermes-agent** — installed via the upstream
  `scripts/install.sh`. Runs as user `agent`, state lives in
  `~/.hermes`. Gateway boots with the OpenAI-compatible API server
  platform enabled on `127.0.0.1:8642` (internal only).
- **molecule_runtime** — our A2A server + bridge adapter. Listens on
  `:8000` and forwards every incoming message to the local hermes-agent
  gateway. The rest of the platform (canvas, plugins, skills installer)
  sees the same A2A contract as any other runtime.

This template was rewritten in v2.0.0 — the previous version was a thin
OpenAI-compat provider shim that shared the `hermes` name with the real
project but had none of its agent capabilities (skills, memory, tools,
self-improvement loop, multi-platform gateway). See
[`docs/PLANNING.md`](./docs/PLANNING.md) for the full rewrite
rationale.

## Usage

### In Molecule AI canvas

Select this template when creating a new workspace — the canvas
Runtime dropdown resolves `hermes` to `workspace-template:hermes`
via `molecule-monorepo/workspace-server/internal/provisioner/provisioner.go`.

### From a URL (community install)

```text
github://Molecule-AI/template-hermes
```

## Required environment

At least one provider key must be set, matching whichever model you
select in the Config tab. hermes-agent picks the right one by
prefix — you do **not** pick the provider yourself.

| Env var              | Used for                                        |
|----------------------|-------------------------------------------------|
| `HERMES_API_KEY`     | Nous Portal (Hermes 3/4 direct)                 |
| `OPENROUTER_API_KEY` | Anything via OpenRouter (200+ models)           |
| `ANTHROPIC_API_KEY`  | Claude direct (native SDK inside hermes-agent)  |
| `OPENAI_API_KEY`     | GPT direct                                      |
| `GEMINI_API_KEY`     | Gemini direct (native SDK inside hermes-agent)  |
| `MINIMAX_API_KEY`    | MiniMax direct                                  |

Set these as workspace-level secrets (`POST /settings/secrets`) — see
`molecule-monorepo/docs/runbooks/saas-secrets.md` for the canonical
flow.

## Persisting skills and memory

`hermes-agent` writes to `~/.hermes` (`/home/agent/.hermes` in the
container). Mount this path as a persistent volume if you want skills,
memory, and cron schedules to survive workspace restarts — the
platform's default Docker named volume does this automatically as long
as the workspace isn't re-provisioned from scratch.

## Files

| File                 | Purpose                                             |
|----------------------|-----------------------------------------------------|
| `Dockerfile`         | Builds the image (hermes-agent + molecule_runtime)  |
| `start.sh`           | Boots hermes gateway, waits for :8642, exec's runtime |
| `adapter.py`         | `HermesAgentAdapter(BaseAdapter)` — just a factory  |
| `executor.py`        | `HermesAgentProxyExecutor` — A2A → hermes HTTP bridge |
| `config.yaml`        | Template metadata + model list for the Config tab   |
| `requirements.txt`   | Python deps for the bridge (molecule_runtime + httpx) |
| `docs/PLANNING.md`   | Rewrite plan + rationale + phase breakdown          |
| `docs/ARCHITECTURE.md` | How the bridge works, port map, failure modes     |
| `docs/MIGRATION.md`  | Upgrade path from v1.x (the old adapter shim)       |
| `docs/CONFIGURATION.md` | How to pick a model, rotate keys, tune hermes-agent |

## Schema version

`template_schema_version: 1` — compatible with Molecule AI platform v1.x.

## License

Business Source License 1.1 — © Molecule AI. `hermes-agent` itself is
MIT-licensed by Nous Research and installed from its upstream repo at
build time.
