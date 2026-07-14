# Support notes — Hermes workspace template

The live issue tracker is the source of truth for open defects:

<https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-hermes/issues>

The previous contents described an obsolete adapter implementation, invented
ticket identifiers, and unsupported heartbeat/config-overlay workarounds. They
are available in git history but are not current operating guidance.

## Gateway readiness

`start.sh` starts `hermes gateway` on loopback port 8642 and waits up to 120
seconds for `/health`. A boot failure prints the gateway log tail. Diagnose the
gateway log and the provider configuration rendered by `start.sh`; do not run a
second adapter process or patch a heartbeat interval.

## Provider selection

Model/provider behavior is defined by `config.yaml`, `start.sh`, and the
provider helper scripts. When multiple credentials exist, the resolved
platform provider or an explicit `HERMES_INFERENCE_PROVIDER` avoids ambiguous
auto-detection. See `docs/CONFIGURATION.md` for the supported variables.

## Compatibility host installer

`install.sh` is retained because downstream compatibility tests exercise it.
It is not evidence of a current production host-deployment path. The supported
Molecule image boots through `Dockerfile` and `start.sh`.

## Timeouts

The bridge request timeout is a code constant in `executor.py`; there is no
documented runtime environment override. Change it only with tests and a pull
request.
