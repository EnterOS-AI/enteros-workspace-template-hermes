# Molecule AI workspace template — Hermes

This repository builds the `hermes` workspace image used by Molecule AI. It
runs Nous Research's upstream
[`hermes-agent`](https://github.com/NousResearch/hermes-agent) gateway behind
the common Molecule A2A runtime.

The canonical source is this Gitea repository. Create workspaces through the
canvas runtime picker; the old URL-based community-install examples are not a
supported installation path.

## Runtime shape

```text
Molecule A2A (:8000)
        |
        v
HermesAgentProxyExecutor
        |
        v
hermes-agent gateway (127.0.0.1:8642)
```

- `start.sh` prepares the container, starts `hermes gateway`, waits for its
  health endpoint, then executes `molecule-runtime` as the `agent` user.
- `adapter.py` provides `HermesAgentAdapter` and platform/plugin hooks.
- `executor.py` proxies A2A turns to the loopback Hermes gateway.
- `config.yaml` is the template's model/provider source. The files under
  `internal/providers/` are a CI-checked registry projection.

`install.sh` remains as a tested compatibility hook for downstream host
installers. It is not the current Molecule production delivery path; the
published container uses `Dockerfile` and `start.sh`.

## Configuration

Select a model through the workspace configuration and provide the credential
declared for that provider. `start.sh` renders the effective Hermes model and
provider configuration and forwards only the supported credential variables.
See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for the current matrix.

The current container renders Hermes state under `/tmp/.hermes`; platform
history is reattached when a fresh gateway has no local transcript. Do not
claim a different home-directory mount is the active persistence contract.

## Important files

| Path | Purpose |
|---|---|
| `Dockerfile` | Builds the published workspace image |
| `start.sh` | Supported container entrypoint |
| `adapter.py` | Adapter and runtime integration |
| `executor.py` | A2A-to-Hermes gateway bridge |
| `config.yaml` | Template metadata, providers, models, and bridge settings |
| `scripts/` | Provider/config helpers and their shell tests |
| `tests/` | Adapter, release, provenance, and documentation contracts |

The current file contains `template_schema_version: 1`; change it only with a
corresponding platform contract change and validation.

## Upstream freshness

The effective hermes engine is the **stock upstream wheel**, pinned as
`ARG HERMES_VERSION` in the Dockerfile. The Molecule A2A integration lives
entirely in the [`hermes-platform-molecule-a2a`](https://git.moleculesai.app/molecule-ai/hermes-platform-molecule-a2a)
plugin, which registers through upstream's `ctx.register_platform(...)`
socket (NousResearch #17751). There is **no patched fork** — the interim
`molecule-ai/hermes-agent` fork was retired on 2026-07-22 (#294) once the
plugin migrated to the upstream API.

A daily bot (`.gitea/workflows/upstream-sync.yml`, 06:17 UTC + manual
dispatch) checks PyPI for a newer `hermes-agent` release and opens a bump
PR against the pin. Bump PRs go through the normal per-PR CI (image build,
prebake self-check, adapter-socket conformance) — the bot only surfaces
work, it never gates or lands anything.

## Development and delivery

See [`runbooks/local-dev-setup.md`](runbooks/local-dev-setup.md) for commands
that mirror CI. Pull requests run static, shell, adapter-conformance, and image
checks. A push to `main` invokes `publish-image`, which publishes to the Gitea
OCI registry and runs the configured pin verification. Do not use a manual
registry script or direct-main-push release procedure.

## License

Business Source License 1.1 — © Molecule AI. The upstream `hermes-agent`
project is installed under its own license.
