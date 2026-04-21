# Runbook: Local Development Setup — hermes Workspace Template

Use this runbook to set up a local development environment for the hermes workspace
template. It covers cloning, dependency installation, running the adapter outside
Docker, config overrides for dev, building the container, and diagnosing common
problems.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | |
| pip | 23+ | |
| Docker | 24+ | |
| Docker Compose | v2 | |
| Git | 2.40+ | |
| Redis (optional) | 7+ | Only needed when testing with `event_log.backend: redis` |
| Molecule platform access | Token with `workspace:dev` scope | |

---

## Step 1 — Clone the Repository

```bash
git clone https://github.com/your-org/molecule-ai-workspace-template-hermes.git
cd molecule-ai-workspace-template-hermes
```

Create a local development branch:

```bash
git checkout -b feat/your-feature-name
```

---

## Step 2 — Install Dependencies

```bash
pip install -r requirements.txt
```

For an isolated virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .\.venv\Scripts\Activate.ps1  # Windows
pip install -r requirements.txt
```

Verify hermes is importable:

```bash
python -c "import hermes; print(hermes.__version__)"
```

---

## Step 3 — Configure Environment Variables

```bash
cp .env.example .env.local
```

Edit `.env.local` with your credentials:

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...
MOLECULE_PLATFORM_URL=https://platform.molecule.ai
MOLECULE_WORKSPACE_ID=ws-dev-local

# Optional — event log persistence (redis)
HERMES_EVENT_LOG_URL=redis://localhost:6379/0

# Optional — concurrency limit
HERMES_MAX_CONCURRENT_TASKS=1

# Optional — log verbosity
LOG_LEVEL=DEBUG
```

> **Security note:** `.env.local` is gitignored. Never commit API keys or tokens.

---

## Step 4 — Config Overrides for Development

Create `config.dev.yaml` to override production defaults locally:

```yaml
# config.dev.yaml — hermes local development overrides

runtime:
  event_log:
    backend: memory       # no redis needed for local dev
    ttl_seconds: 600
  max_turns_per_task: 10  # faster test cycles

model:
  temperature: 0.8        # more exploratory
  max_tokens: 4096        # lower latency for local testing

observability:
  heartbeat_interval_seconds: 30
  log_level: DEBUG
```

Run the adapter with both configs merged:

```bash
python adapter.py --config config.yaml --config-override config.dev.yaml
```

The dev overrides take precedence for any conflicting keys.

---

## Step 5 — Run the Adapter Locally

```bash
python adapter.py
```

Expected startup output:

```
hermes adapter v0.8.2 (runtime=hermes)
  platform : https://platform.molecule.ai
  workspace: ws-dev-local
  model    : claude-sonnet-4-6 (max_tokens=4096)
  event_log: memory (ttl=600s)
  heartbeat: 30s interval

[hermes] INFO  — config loaded (schema_version=1.1)
[hermes] INFO  — skills loaded from: /opt/molecule/skills
[hermes] INFO  — event loop running — press Ctrl+C to stop
```

The adapter will begin polling the platform for inbound events. Stop with `Ctrl+C`.

---

## Step 6 — Docker Build and Smoke Test

Build the dev image:

```bash
docker build -t molecule-hermes-workspace:dev .
```

Run a quick smoke test (verifies the adapter starts without crashing):

```bash
docker run --rm \
  --env-file .env.local \
  molecule-hermes-workspace:dev \
  python -c "
from adapter import HermesAdapter
a = HermesAdapter()
a.load_config()
print('smoke test PASSED')
"
```

Full Docker Compose stack (with Redis for event log persistence):

```bash
docker compose up --build
```

Tail logs:

```bash
docker compose logs -f workspace
```

Teardown:

```bash
docker compose down
```

To also remove the Redis volume:

```bash
docker compose down -v
```

---

## Common Issues Table

| Symptom | Likely Cause | Resolution |
|---|---|---|
| `ModuleNotFoundError: No module named 'hermes'` | `requirements.txt` not installed | Run `pip install -r requirements.txt` |
| Adapter exits immediately with code 0 | Hermes version mismatch with platform | Pin `hermes` in `requirements.txt` to platform version; see known-issues.md |
| `ValidationError: config schema version '1.0' is not supported` | `schema_version` too old | Update `config.yaml` to minimum supported by platform |
| Workspace shows "inactive" in platform dashboard | HEARTBEAT not forwarded | Set `heartbeat_interval_seconds: 60` in `config.yaml`; upgrade to v0.7.2+ |
| `redis.exceptions.ConnectionError` on startup | `HERMES_EVENT_LOG_URL` set but redis not running | Start redis: `docker compose up -d redis`; or set `backend: memory` in dev overrides |
| `system-prompt.md` appears to be ignored | Prompt exceeds token limit | Check token count: see known-issues.md; keep file under 8,000 tokens |
| Model override in `config.yaml` not respected | Platform override being overwritten by local config | Use `config.dev.yaml` with env var substitution instead; see known-issues.md |
| `docker build` fails at `pip install` step | Corporate proxy / network restriction | Set pip index mirror: `pip install --index-url https://pypi.org/simple/ -r requirements.txt` |
| `docker run` crashes with `OMP_NUM_THREADS` warning | Missing CPU thread config | Add `OMP_NUM_THREADS=1` to `.env.local` and pass with `--env-file` |
