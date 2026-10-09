# Historical design record — real Hermes agent bridge

> Historical only. The rewrite described here has shipped. Do not use this file
> as a release checklist or a statement of unfinished production work.

The original plan corrected a name/behavior mismatch: the old `hermes` template
was a thin completion-provider shim, while users expected the upstream Hermes
agent runtime. The completed design keeps Molecule's A2A surface stable while
running the real Hermes gateway behind it.

## Decisions that still apply

- `start.sh` owns gateway boot and readiness.
- `adapter.py` stays thin and does not duplicate provider selection.
- `executor.py` owns the loopback HTTP bridge.
- The gateway is internal to the workspace; A2A remains the platform-facing
  protocol.
- The template does not reintroduce the removed provider/escalation modules.

Current files and tests, not the original phase plan, are authoritative. Open
future work as Gitea issues instead of appending aspirational phases here.
