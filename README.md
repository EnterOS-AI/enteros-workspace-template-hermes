# template-hermes

Molecule AI workspace template that runs the **real Nous Research
[hermes-agent](https://github.com/NousResearch/hermes-agent)** behind an
A2A bridge.

## What's actually in the workspace

Both the Docker path and the SaaS bare-host path run the same stack:

- **hermes-agent** — real upstream project, installed via
  `scripts/install.sh` from NousResearch/hermes-agent. Gateway boots
  with the OpenAI-compatible API server platform enabled on
  `127.0.0.1:8642` (internal only).
- **molecule_runtime** — A2A server + bridge adapter. Listens on
  `:8000` and forwards every incoming message to the local
  hermes-agent gateway. Canvas, plugins, skills installer see the
  same A2A contract as any other runtime.

## Two execution paths

This template ships two entrypoints because the platform has two
execution models — see
[internal/product/designs/workspace-backends.md](https://git.moleculesai.app/molecule-ai/internal/src/branch/main/product/designs/workspace-backends.md)
for the full story.

| Path | Used by | Entry | Install recipe |
|---|---|---|---|
| Docker (1 container / workspace) | `docker compose up` local dev | `ENTRYPOINT ["start.sh"]` in `Dockerfile` | Image build: `RUN curl install.sh \| bash` in `Dockerfile` |
| Bare host (1 EC2 / workspace) | SaaS production | `/opt/molecule-venv/bin/molecule-runtime` (CP user-data) | `install.sh` runs at workspace-provision time as `ubuntu` user |

`start.sh` and `install.sh` do the same logical work (install
hermes-agent, seed `~/.hermes/.env` + `config.yaml`, start `hermes
gateway`, wait for `:8642`). They stay symmetric; when you change
one, check the other.

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
| `Dockerfile`         | Docker-path: builds the image (hermes-agent + molecule_runtime) |
| `start.sh`           | Docker-path entrypoint: boots hermes gateway, waits for :8642, exec's runtime |
| `install.sh`         | Bare-host path (EC2/SaaS): runs at provision time as the runtime user. Installs hermes-agent + starts gateway in background. Called by CP user-data after pip-install of molecule_runtime, before molecule-runtime launches. |
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
