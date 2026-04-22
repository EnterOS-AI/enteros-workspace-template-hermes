# Architecture — A2A bridge to hermes-agent

## Port map

```
┌─────────────────── workspace container ───────────────────┐
│                                                           │
│   :8000  ← molecule_runtime (A2A server + adapter) ──┐    │
│                                                      │    │
│                       proxy                          │    │
│                                                      ▼    │
│   :8642  → hermes-agent gateway (OpenAI-compat API)       │
│            running as user `agent`, state in ~/.hermes    │
│                                                           │
└───────────────────────────────────────────────────────────┘
            ▲
            │  (only :8000 exposed outside)
            │
      platform + canvas
```

- **`:8000`** — A2A server, exposed to the rest of the platform.
  Contract is stable across all runtimes (langgraph, claude-code,
  hermes, etc.).
- **`:8642`** — hermes-agent's OpenAI-compatible HTTP API. Loopback
  only. Never routed outside the container.

## Boot sequence

`start.sh` (runs as root inside the container):

1. Generate a random `API_SERVER_KEY` if the env var isn't already
   set. This is hermes-agent's bearer token; the executor reads it
   from the env at request time.
2. Write `/home/agent/.hermes/.env`:
   - `API_SERVER_ENABLED=true`
   - `API_SERVER_KEY=<generated>`
   - `API_SERVER_HOST=127.0.0.1`
   - `API_SERVER_PORT=8642`
   - Any provider keys present in the container env (`HERMES_API_KEY`,
     `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
     `GEMINI_API_KEY`, `MINIMAX_API_KEY`) forwarded through.
3. Launch `hermes gateway` in the background as user `agent` via
   `sudo -u agent -E bash -lc 'hermes gateway'`. Logs → `/var/log/hermes-gateway.log`.
4. Poll `http://127.0.0.1:8642/health` up to 60×1s. Fail loud on
   timeout — dumps last 80 log lines to stderr so provisioning logs
   capture the reason.
5. `exec molecule-runtime` — replaces the shell, becoming PID 1.
   molecule-runtime loads `Adapter = HermesAgentAdapter` from
   `__init__.py` and starts the A2A server on `:8000`.

## Request flow

```
canvas ─── POST /a2a/... ───▶ molecule_runtime (:8000)
                                    │
                                    ▼
                        HermesAgentProxyExecutor.execute()
                                    │
                 ┌──────────────────┴──────────────────┐
                 ▼                                     ▼
      extract message text                  build {model, messages[], stream:false}
                                                      │
                                                      ▼
                                  POST 127.0.0.1:8642/v1/chat/completions
                                  Authorization: Bearer ${API_SERVER_KEY}
                                                      │
                                                      ▼
                                  hermes-agent runs the turn with its
                                  native tools (terminal, files, web,
                                  memory, skills), resolves provider
                                  from the `model` string, returns
                                  OpenAI-format response.
                                                      │
                                                      ▼
                           extract choices[0].message.content
                                                      │
                                                      ▼
                              event_queue.enqueue_event(
                                new_agent_text_message(...)
                              )
                                                      │
                                                      ▼
                                            canvas receives the reply
```

## What the bridge is intentionally **not** doing

- **Provider selection.** The bridge sends the `model` string
  verbatim. hermes-agent owns the registry and picks provider + API
  keys. If you ever feel tempted to add fallback chains here, stop —
  that's a regression to v1.x.
- **Tool routing.** Tools are hermes-agent's job. Our bridge sees
  only the final assistant text.

## Provider routing (how keys become inference)

Provider resolution happens inside hermes-agent, driven by:

1. **`~/.hermes/cli-config.yaml`** — `model.provider` field. start.sh
   seeds this file on first boot (`auto` by default, or whatever
   `HERMES_INFERENCE_PROVIDER` specifies).
2. **`~/.hermes/.env`** — every provider key we forward from the
   container env (see start.sh for the full list; see
   `CONFIGURATION.md#provider-matrix` for the mapping).
3. **Auto-detection** — when `provider: auto`, hermes walks its
   internal resolution order and picks the first provider whose
   credential is present. When multiple keys are set, prefer explicit
   `HERMES_INFERENCE_PROVIDER` to avoid surprises.

### Common routing gotcha

With only `OPENAI_API_KEY` set and `provider: auto`, hermes-agent will
route to `openai-codex` (Codex API, OAuth-only) and return:

```
401 - Missing Authentication header
```

The fix is to set `HERMES_INFERENCE_PROVIDER=openrouter` — hermes's
openrouter provider accepts `OPENAI_API_KEY` as alt-auth and routes
OpenAI-format Chat Completions correctly. This is documented in
`CONFIGURATION.md#forcing-a-provider`.

### Auxiliary model

Vision, web summarization, and MoA use a separate auxiliary model —
defaults to Gemini Flash via OpenRouter. If `OPENROUTER_API_KEY` is
absent, these capabilities break silently (the primary path still
works). Set `HERMES_AUXILIARY_PROVIDER` to override.
- **Streaming.** `stream: false` in the request payload. A later
  revision can upgrade to SSE by subscribing to
  `GET /v1/runs/{run_id}/events` and pushing partial messages into
  the A2A event queue — the `AgentExecutor` contract already
  supports multiple `enqueue_event` calls per turn.
- **Caching.** hermes-agent has its own session store
  (`X-Hermes-Session-Id`); the bridge does not attempt to pin
  conversations. Molecule's A2A layer already carries session
  context in its message envelope.

## Failure modes

| Symptom                                             | Likely cause                                  | Where to look                                                  |
|-----------------------------------------------------|-----------------------------------------------|---------------------------------------------------------------|
| Provisioning fails at "health probe"                | `hermes gateway` crashed during boot          | `/var/log/hermes-gateway.log` (tail in start.sh stderr dump)  |
| Every request returns `[hermes-agent error 401]`    | `API_SERVER_KEY` mismatch between processes   | Inspect `/home/agent/.hermes/.env` + container env            |
| Every request returns `[hermes-agent error 400]`    | Model string unrecognized by hermes-agent     | `docker exec -u agent … hermes model` inside the container    |
| `[hermes-agent unreachable]`                        | Gateway exited post-boot (OOM, crash)         | `/var/log/hermes-gateway.log`; may need container restart     |
| Skills disappear between sessions                   | `/home/agent/.hermes` not volume-mounted      | Platform-side volume config; see `CONFIGURATION.md`            |
| Agent ignores provider key                          | Key not forwarded from container env          | Workspace secrets; see runbooks/saas-secrets.md in monorepo   |

## Future work

- **Streaming:** subscribe to `/v1/runs/.../events` and pipe partial
  assistant tokens + tool progress into the A2A queue.
- **Session pinning:** thread hermes-agent's `X-Hermes-Session-Id`
  through the A2A envelope so long conversations keep server-side
  context when beneficial.
- **`hermes config set` sync:** when canvas Config tab changes the
  `model` field, also invoke `hermes model <id>` so CLI usage inside
  the workspace's Terminal tab stays in sync.
- **Gateway platforms passthrough:** let customers opt their workspace
  into hermes-agent's Telegram/Discord/Slack platforms without
  duplicating the config surface.
- **Install pin:** once stabilized, replace `curl install.sh | bash`
  in the Dockerfile with a pinned commit SHA so a bad upstream
  release doesn't break builds.
