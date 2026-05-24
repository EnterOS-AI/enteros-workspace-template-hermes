# Coding Discipline (Karpathy 4)

All code changes in this workspace follow these principles:

1. **Think Before Coding** — State assumptions explicitly. If unclear, ask, don't guess.
2. **Simplicity First** — Minimum code that solves the problem. No speculative abstractions.
3. **Surgical Changes** — Only touch what the task requires. Match existing style exactly.
4. **Goal-Driven Execution** — Define verifiable success criteria before implementing.

For concrete anti-pattern examples, see the `coding-discipline` skill or `EXAMPLES.md` in the Karpathy guidelines repo.

---

# Molecule AI Workspace Template — hermes

## Purpose

This is a **workspace template** for the hermes runtime. It provides a pre-configured
workspace environment (Dockerfile, config.yaml, adapter.py, system-prompt.md, and
supporting files) that Molecule AI agents run inside. It is NOT a plugin — it has no
`plugin.yaml` and no `rules/` directory.

Use this template when you want to deploy a lightweight, event-driven agentic
workspace with the hermes runtime.

---

## Key Files and Their Roles

| File | Role |
|---|---|
| `config.yaml` | Runtime configuration: schema version, model, runtime (hermes), event routing, skill paths, env-var bindings |
| `adapter.py` | Event-loop adapter for the hermes runtime. Manages the agent lifecycle, routes inbound events, streams responses, and forwards HEARTBEAT events to the platform |
| `system-prompt.md` | System-level instructions injected into every agent turn (identity, goals, output format, safety guardrails) |
| `requirements.txt` | Python dependencies (hermes SDK, platform client, LLM client, async utilities) |
| `Dockerfile` | Container image definition for the hermes runtime environment |

---

## Runtime Configuration Conventions

All runtime configs live in `config.yaml`. The hermes runtime uses an event-sourcing
model: inbound events (task assignments, tool results, platform signals) are written
to an in-memory event log and processed by a single `AgentLoop` coroutine.

```yaml
schema_version: "1.1"

runtime:
  name: hermes
  version: "0.8"            # must match installed hermes package major.minor
  event_log:
    backend: memory         # "memory" for dev, "redis" for prod
    ttl_seconds: 3600
  max_turns_per_task: 50

model:
  provider: anthropic
  name: claude-sonnet-4-6
  max_tokens: 8192
  temperature: 0.5
  system_prompt_file: system-prompt.md

tools:
  registry: builtin
  allowed_tools:
    - code_interpreter
    - file_read
    - file_write
    - bash

skills:
  load_paths:
    - /opt/molecule/skills
  auto_init: true

observability:
  heartbeat_interval_seconds: 60
  log_level: INFO
```

---

## Environment Variables Expected

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | API key for the LLM provider |
| `MOLECULE_PLATFORM_URL` | Yes | Base URL of the Molecule platform (e.g. `https://platform.molecule.ai`) |
| `MOLECULE_WORKSPACE_ID` | Yes | Workspace instance ID assigned by the platform |
| `HERMES_EVENT_LOG_URL` | No | Redis URL for persisted event log in multi-instance deployments |
| `LOG_LEVEL` | No | Python log level override (`DEBUG`, `INFO`, `WARNING`) |
| `HERMES_MAX_CONCURRENT_TASKS` | No | Maximum number of simultaneous task loops (default: 1) |

---

## Skill Loading

The hermes adapter loads skills at startup from paths declared in `config.yaml` under
`skills.load_paths`. Each path must contain a `skill.yaml` manifest that declares
runtime compatibility. The adapter evaluates `runtime: hermes` or `runtime: "*"` to
decide whether to activate a skill.

Loading is synchronous during startup. If `skills.auto_init: false`, skills are not
loaded automatically; the adapter's `BOOTSTRAP` phase (if added) must call
`adapter.load_skills()` manually before entering the event loop.

---

## Development Setup

### Prerequisites

- Python 3.11+
- Docker 24+ / Docker Compose v2
- Git

### Clone and install

```bash
git clone https://github.com/your-org/molecule-ai-workspace-template-hermes.git
cd molecule-ai-workspace-template-hermes
pip install -r requirements.txt
```

### Run adapter locally (outside Docker)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export MOLECULE_PLATFORM_URL="https://platform.molecule.ai"
export MOLECULE_WORKSPACE_ID="ws-dev-local"

python adapter.py
```

The adapter will emit a startup banner with the resolved config and begin processing
the event loop.

### Test the Docker build

```bash
docker build -t molecule-hermes-workspace:dev .
docker run --rm \
  -e ANTHROPIC_API_KEY \
  -e MOLECULE_PLATFORM_URL \
  -e MOLECULE_WORKSPACE_ID \
  molecule-hermes-workspace:dev
```

### Docker Compose

```yaml
# docker-compose.yml
version: "3.9"
services:
  workspace:
    build: .
    env_file: .env.local
    volumes:
      - ./config.yaml:/workspace/config.yaml:ro
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

Run with:

```bash
docker compose up --build
```

---

## Testing

| Test type | Command | Notes |
|---|---|---|
| Unit (adapter) | `pytest tests/test_adapter.py -v` | Mocks the platform event stream; tests event routing, heartbeat scheduling |
| Unit (event log) | `pytest tests/test_event_log.py -v` | Tests serialization, TTL eviction, back-pressure |
| Integration | `docker compose -f compose.test.yml up --abort-on-container-exit` | Full stack; requires `.env.test` |
| Lint | `ruff check .` | Must pass before release |

---

## Release Process

1. **Update `config.yaml` schema version** to match the target platform release:

   ```yaml
   schema_version: "1.2"
   ```

2. **Bump `runtime.version`** in `config.yaml` and confirm the corresponding
   `hermes` package in `requirements.txt` satisfies it:

   ```
   hermes>=0.8.0,<0.9.0
   ```

3. **Run the full test suite** — all tests must pass.

4. **Tag the release:**

   ```bash
   git tag -a v0.8.2 -m "release: align with platform schema 1.2"
   git push origin main
   git push origin v0.8.2
   ```

5. **Update CHANGELOG.md** with config, runtime, and adapter changes.

---

## Note: This Is a Workspace Template, Not a Plugin

This template does **not** contain a `plugin.yaml` or a `rules/` directory. Plugins
extend the agent's capability set at runtime and are declared in `config.yaml` under
`skills`. This template only provides the **environment** — the hermes event loop,
config, and system prompt — in which the agent runs.
