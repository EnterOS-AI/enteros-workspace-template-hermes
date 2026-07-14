# Historical migration record — Hermes v1 to v2

> Historical only. This is not an active deployment or rollback runbook. For
> the current image, configuration, and validation paths, use the repository
> `README.md`, `docs/CONFIGURATION.md`, and `runbooks/local-dev-setup.md`.

The v2 rewrite replaced the repository's former text-completion shim with the
real `hermes-agent` gateway. The durable architecture that remains is:

- `molecule-runtime` serves the platform A2A contract on port 8000.
- `hermes gateway` runs on loopback port 8642.
- `HermesAgentProxyExecutor` bridges A2A turns to the gateway.
- Provider/model declarations live in `config.yaml` and the Hermes boot
  helpers, not in the removed `providers.py` or `escalation.py` modules.

Old version-specific upgrade, canary, and manual rollback steps were removed
because they referenced repositories and delivery paths that are no longer
current. Git history preserves the original migration narrative when forensic
context is needed.
