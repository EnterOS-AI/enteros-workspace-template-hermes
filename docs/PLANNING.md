# Rewrite Plan — v2.0.0: run the real hermes-agent

## Context

The `hermes` runtime in Molecule AI used to be a thin OpenAI-compat
provider shim that dispatched chat-completions to Nous Portal,
OpenRouter, Anthropic, Gemini, and ~12 other providers. It shared the
name with [Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent)
but had **none** of the agent capabilities:

| Capability                         | Old `template-hermes` v1.x | Real `hermes-agent` |
|------------------------------------|----------------------------|---------------------|
| Terminal / file / web tools        | No                         | Yes (native)        |
| Skills with self-improvement loop  | No                         | Yes                 |
| Cross-session memory / FTS search  | No                         | Yes                 |
| Telegram/Discord/Slack gateway     | No                         | Yes                 |
| Scheduled automations (cron)       | No                         | Yes                 |
| Sub-agent spawning                 | No (platform-level only)   | Yes                 |
| Provider registry                  | Duplicated in `providers.py` | Owned by hermes-agent (`hermes model`) |

Customers picking "Hermes" in the canvas expected the agent framework —
they got a stateless chat shim. This PR replaces the shim with the real
thing and removes the name collision.

## Goals

1. **Drop the shim.** Delete `providers.py`, `escalation.py`, and the
   multi-provider dispatch logic in the old `executor.py`. hermes-agent
   already owns all of that.
2. **Install the real agent.** `curl install.sh | bash` at image-build
   time, same way `template-claude-code` pulls the real claude CLI.
3. **Preserve the A2A contract.** molecule_runtime still serves `:8000`;
   the rest of the platform doesn't need to know hermes-agent exists.
4. **Own nothing hermes-agent owns.** No provider selection, no
   fallback chains, no model alias mapping. The template is just
   transport glue.

## Non-goals

- ACP (`acp_adapter/server.py`) integration. hermes-agent exposes it,
  but our platform speaks A2A; adding a second protocol surface isn't
  worth the complexity today.
- Streaming. Buffering a final response is fine for agent-turn
  latency (tool-using turns dominate wall time, not token output).
  Streaming upgrade path is noted in `ARCHITECTURE.md`.
- Telegram/Discord/Slack gateway exposure. The platform's entry
  surface is canvas; those platforms are out of scope for now.
- Migration shim for v1.x configs. v1.x had no production customers,
  only CI canary runs.

## Scope — what changes

### Added
- `start.sh` — boots `hermes gateway` (api_server platform), waits for
  `:8642` health, exec's `molecule-runtime`.
- `docs/PLANNING.md` (this file)
- `docs/ARCHITECTURE.md`
- `docs/MIGRATION.md`
- `docs/CONFIGURATION.md`

### Rewritten
- `Dockerfile` — installs hermes-agent via upstream installer, copies
  bridge files, sets entrypoint to `start.sh`.
- `adapter.py` — shrinks from multi-provider dispatch to a factory that
  returns `HermesAgentProxyExecutor`.
- `executor.py` — pure HTTP proxy: A2A message in → POST /v1/chat/completions
  → A2A message out.
- `config.yaml` — `runtime: hermes`, v2.0.0, cleaner model list.
  Canvas Config tab resolves `required_env` per model from the
  registry.
- `requirements.txt` — `molecule-ai-workspace-runtime` + `httpx`.
  Dropped `openai`, `anthropic`, `google-genai` — hermes-agent owns
  provider SDKs.
- `__init__.py` — exports `HermesAgentAdapter` instead of
  `HermesAdapter`.
- `README.md` — reflects the new reality.

### Deleted
- `providers.py` (replaced by hermes-agent's internal registry)
- `escalation.py` (replaced by hermes-agent's skill/memory loop)

### Unchanged
- `CLAUDE.md` — workspace-level agent instructions, orthogonal.
- `known-issues.md` — retained for historical context.
- `runbooks/` — platform-level runbooks, still applicable.
- `.molecule-ci/` — CI metadata.

## Phases

### Phase 1 — Merge this PR
Lands the rewrite. Next image build publishes
`workspace-template:hermes` as the new v2.0.0 base.

### Phase 2 — Validate in staging
Re-provision the staging canary workspace
(`.github/workflows/canary-staging.yml` → `E2E_RUNTIME=hermes`) and
verify the agent can:
- Start (health probe green)
- Respond to a trivial `hello` prompt end-to-end via A2A
- Use at least one built-in tool (`hermes tools` → terminal/file)
- Surface hermes-agent errors cleanly when provider keys are absent

### Phase 3 — Persist skills + memory
The default Docker named volume the platform attaches already
covers `/home/agent/.hermes`. Confirm that skills created in one
canvas session survive a container restart. Document in
`CONFIGURATION.md` which mount points matter.

### Phase 4 — Opt into hermes-agent gateway platforms
(Out of scope for v2.0.0.) Later release can expose hermes-agent's
Telegram/Discord/Slack gateway to customers by surfacing those
platforms in the canvas Config tab and wiring secrets in. Tracked
separately — will open an issue when Phase 3 lands.

## Risks + mitigations

| Risk                                                           | Mitigation                                                                 |
|----------------------------------------------------------------|----------------------------------------------------------------------------|
| hermes-agent's install.sh pulls a newer agent schema that doesn't match our bridge | Pin the upstream install commit in the Dockerfile once the bridge is battle-tested. Today we're on `main` intentionally — Nous moves fast and we want those features. |
| `:8642` bind collides with something the user spawns           | Internal loopback only, and the port is documented in `ARCHITECTURE.md`.   |
| hermes-agent crashes mid-request                               | Bridge surfaces the HTTP error as an A2A message; gateway restart is a manual ops step (see `CONFIGURATION.md#restart`). |
| `API_SERVER_KEY` regenerated on each boot invalidates old clients | Clients are us; we read it from env at request time, not cached.           |
| Skills/memory loss on workspace re-provision                   | Covered in Phase 3 — volume mount at `/home/agent/.hermes`.                |

## Success criteria

- `workspace-template:hermes` image built and pushed.
- Staging canary goes green on a hermes workspace.
- `hermes` runtime in canvas picks up real agent capabilities (skills
  visible, terminal tools usable in agent output).
- `providers.py` / `escalation.py` grep clean in the repo.

## Open questions

1. Should we expose `hermes` CLI subcommands (`hermes model`,
   `hermes tools`, `hermes skills`) through the canvas Terminal tab
   directly, or keep them hidden behind the A2A chat interface? Leaning
   Terminal tab — operators will want to configure without writing a
   natural-language prompt.
2. Where does the per-workspace model selection surface live? Today
   it's `AdapterConfig.model` → payload `model` field. If a user
   switches in the canvas, do we also write it to `~/.hermes/config.yaml`
   via `hermes config set` so CLI usage stays in sync? Probably yes;
   follow-up PR.
