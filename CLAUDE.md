# Coding discipline

1. Think before coding: verify assumptions against current files and tests.
2. Prefer the smallest change that satisfies the task.
3. Keep edits surgical and match the existing style.
4. Define the validation that proves the change before implementing it.

# Repository guide

This is the `hermes` workspace image, not a plugin. The supported container
path is:

1. `start.sh` prepares `/configs`, the agent home, provider configuration, and
   the management MCP environment.
2. It starts the loopback `hermes gateway` and waits for health on port 8642.
3. It executes `molecule-runtime`, which loads `HermesAgentAdapter` and serves
   A2A on port 8000.
4. `HermesAgentProxyExecutor` forwards turns to the gateway.

Treat these files as the sources of truth:

| Concern | Source |
|---|---|
| Model/provider declarations | `config.yaml` |
| Container boot | `start.sh` |
| Adapter contract | `adapter.py` |
| Turn execution | `executor.py` |
| Runtime dependency | `.runtime-version`, `requirements.txt` |
| Delivery behavior | `.gitea/workflows/publish-image.yml` |
| Supported local checks | `runbooks/local-dev-setup.md` and `.gitea/workflows/ci.yml` |

Do not add a second provider registry, run `adapter.py` as a standalone CLI,
or document unimplemented config-overlay flags. Keep credential values out of
logs and examples. Open a branch and pull request; never push directly to
`main`, tag a release, or manually publish an image as part of routine repo
work.
