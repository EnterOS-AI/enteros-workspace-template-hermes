# Support notes — Hermes workspace template

The live issue tracker is the source of truth for open defects:

<https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-hermes/issues>

The previous contents described an obsolete adapter implementation, invented
ticket identifiers, and unsupported heartbeat/config-overlay workarounds. They
are available in git history but are not current operating guidance.

## Gateway readiness

`start.sh` starts `hermes gateway` on loopback port 8642 and waits up to 420
seconds for `/health` (the first boot of a new hermes on an existing volume
migrates `state.db` first). It fails at once if the gateway exits. A boot
failure prints the gateway log tail. Diagnose the
gateway log and the provider configuration rendered by `start.sh`; do not run a
second adapter process or patch a heartbeat interval.

## Provider selection

Model/provider behavior is defined by `config.yaml`, `start.sh`, and the
provider helper scripts. When multiple credentials exist, the resolved
platform provider or an explicit `HERMES_INFERENCE_PROVIDER` avoids ambiguous
auto-detection. See `docs/CONFIGURATION.md` for the supported variables.

## Hermes no longer resets sessions on a timer

Upstream commit `1d5d0594` ("stop time-triggered conversation rotation"),
included in the pinned `HERMES_COMMIT`, made `session_reset:` in `config.yaml`
and the `SESSION_IDLE_MINUTES` / `SESSION_RESET_HOUR` variables inert. A
gateway conversation lasts until something sends `/new`. On a scheduled
workspace nothing does, and a long-lived session drifts toward its own
compacted history (#383). Writing `session_reset:` into the generated config
does not change this. Use `HERMES_SESSION_RESET_MODE` and its companions
(`docs/CONFIGURATION.md`), which make the executor send `/new` before the turn
that crosses the boundary.

## Compatibility host installer

`install.sh` is retained because downstream compatibility tests exercise it.
It is not evidence of a current production host-deployment path. The supported
Molecule image boots through `Dockerfile` and `start.sh`.

## Timeouts

On the plugin transport (the image default) a turn has no fixed timeout. It
ends when Hermes answers it, when the gateway shows no sign of life for the
stall window, or at the turn cap, which sends Hermes' `/stop`. The workspace
reports busy for as long as a turn runs. A caller still waiting at the reply
deadline gets an interim reply, before the platform's own 30-minute limit, and
the turn's answer follows in the chat. See "Turn supervision" in
`docs/CONFIGURATION.md` for the knobs and what a caller sees. The
chat-completions fallback keeps its fixed request timeout in `executor.py`.
