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

The effective hermes engine is **stock upstream, installed from a pinned git
commit**: `ARG HERMES_COMMIT` in the Dockerfile. That ARG is the whole
reproducibility story, and it is a commit rather than a version for a reason
worth knowing before you touch this: `~/.local/bin/hermes` is not the venv
console script, it is a wrapper that execs `$HERMES_ROOT/hermes`, which puts
the git **checkout** at `sys.path[0]` ahead of site-packages. The gateway runs
the checkout. A wheel pin in site-packages is inert — which is what it was
until 2026-09-03, while the checkout silently tracked whatever upstream `main`
happened to be on build day.

The upstream installer that clones and pins that checkout is vendored at
`vendor/hermes-agent/install.sh` (a byte copy of `scripts/install.sh` at
`HERMES_COMMIT`, checked by sha256 at build time) rather than downloaded, so a
build does not depend on raw.githubusercontent.com. Moving `HERMES_COMMIT`
means refreshing that copy; `vendor/hermes-agent/README.md` has the steps.

The pin is currently an **unreleased upstream ref**, taken with owner
authorisation: PyPI's newest `hermes-agent` is still 0.19.0 (2026-07-20) and
the context-compaction and unattended-approval fixes the fleet needs exist only
as git refs. `ARG HERMES_PYPI_FLOOR` records the lowest published release that
would carry them, and the plan is to return to a tagged release once one
exists.

The Molecule A2A integration lives entirely in the
[`hermes-platform-molecule-a2a`](https://git.moleculesai.app/molecule-ai/hermes-platform-molecule-a2a)
plugin, which registers through upstream's `ctx.register_platform(...)`
socket (NousResearch #17751). There is **no patched fork** — the interim
`molecule-ai/hermes-agent` fork was retired on 2026-07-22 (#294) once the
plugin migrated to the upstream API.

A daily bot (`.gitea/workflows/upstream-sync.yml`, 06:17 UTC + manual
dispatch) compares PyPI's latest `hermes-agent` against `HERMES_PYPI_FLOOR`.
Below the floor it does nothing; at or above it, it files an issue with the
checklist for returning to a published release. It deliberately does **not**
auto-bump `HERMES_COMMIT` to newer git refs — every such bump is unreviewed
third-party code entering a customer image, so the pin moves when someone
decides it should. The bot only surfaces work; it never gates or lands
anything.

## Development and delivery

See [`runbooks/local-dev-setup.md`](runbooks/local-dev-setup.md) for commands
that mirror CI. Pull requests run static, shell, adapter-conformance, and image
checks. A push to `main` invokes `publish-image`, which publishes to the Gitea
OCI registry and runs the configured pin verification. Do not use a manual
registry script or direct-main-push release procedure.

## License

Business Source License 1.1 — © Molecule AI. The upstream `hermes-agent`
project is installed under its own license.
