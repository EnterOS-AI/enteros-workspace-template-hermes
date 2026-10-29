# Hermes workspace configuration

The active configuration is derived by `start.sh` from the workspace's
`/configs/config.yaml`, platform-injected resolution variables, and credential
environment. `config.yaml` in this repository defines the template defaults and
the model/provider choices shown by the platform.

The template defaults to platform-managed MiniMax M3 (`minimax/MiniMax-M3`),
which uses the platform proxy and needs no personal MiniMax key. The separate
`minimax:MiniMax-M3` choice uses `MINIMAX_API_KEY`. Existing explicit model
selections keep their normal precedence.

## Model and provider precedence

1. Platform-resolved model/provider values are authoritative when present.
2. Explicit `HERMES_INFERENCE_MODEL` and `HERMES_INFERENCE_PROVIDER` values are
   honored by the boot helpers.
3. Otherwise the helper derives the provider from the selected model and
   available supported credential.

The rendered Hermes config is written under `/tmp/.hermes` in the current
container. Editing a different home-directory config does not change the booted
gateway.

## Credential variables forwarded by `start.sh`

Provide credentials through the workspace secret configuration. Never put a
real value in this repository or a shell transcript.

| Provider family | Accepted variables |
|---|---|
| Nous | `NOUS_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Gemini | `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| GLM | `GLM_API_KEY` |
| Kimi | `KIMI_API_KEY`, `KIMI_CN_API_KEY` |
| MiniMax | `MINIMAX_API_KEY`, `MINIMAX_CN_API_KEY` |
| Alibaba/Qwen | `DASHSCOPE_API_KEY` |
| Xiaomi | `XIAOMI_API_KEY` |
| Arcee | `ARCEEAI_API_KEY` |
| NVIDIA | `NVIDIA_API_KEY` |
| Ollama Cloud | `OLLAMA_API_KEY` |
| Hugging Face | `HF_TOKEN` |
| AI gateway | `AI_GATEWAY_API_KEY` |
| Kilo Code | `KILOCODE_API_KEY` |
| OpenCode | `OPENCODE_ZEN_API_KEY`, `OPENCODE_GO_API_KEY` |
| Copilot | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` |

`HERMES_API_KEY` is not forwarded as a primary inference credential by the
current boot script; use the provider-specific variable declared above and in
`config.yaml`.

## Platform-managed inference

For a platform-resolved route, the boot helpers translate the injected model,
base URL, and usage credential into the Hermes custom-provider shape. Do not
override that route with an unrelated direct-provider endpoint. The shell tests
`test-derive-platform-llm.sh` and `test-default-model-selection.sh` guard this
behavior.

## Molecule tools and plugins

The boot path writes the native Hermes MCP descriptor for the loopback
`molecule` server. `adapter.py` installs declared plugins through the runtime's
adapter registry and materializes compatible skills/rules before serving. Do
not add a second shell plugin installer.

## Session, memory, and skill-review knobs

Set these as workspace environment variables or secrets. Each is optional and
unset keeps current behaviour. An invalid value is logged at WARNING during
boot and ignored.

| Variable | Effect | Accepted values |
|---|---|---|
| `HERMES_SESSION_RESET_MODE` | Start a fresh Hermes session (Hermes' own `/new`) before the first turn past a boundary | `none` (default), `idle`, `daily`, `both` |
| `HERMES_SESSION_RESET_HOUR` | Daily boundary, container local time (UTC in the platform image) | `0`-`23`; default `4` |
| `HERMES_SESSION_IDLE_MINUTES` | Idle gap since the previous turn finished | integer `>= 1`; default `1440` |
| `HERMES_MEMORY_CHAR_LIMIT` | `memory.memory_char_limit` | integer `>= 1`; Hermes default `2200` |
| `HERMES_USER_CHAR_LIMIT` | `memory.user_char_limit` | integer `>= 1`; Hermes default `1375` |
| `HERMES_MEMORY_NUDGE_INTERVAL` | `memory.nudge_interval`, user turns between background memory reviews | integer `>= 0`, `0` turns the review off; Hermes default `10` |
| `HERMES_SKILL_NUDGE_INTERVAL` | `skills.creation_nudge_interval`, tool iterations between background skill reviews | integer `>= 0`, `0` turns the review off; Hermes default `10` |

`start.sh` writes the memory and skill keys into the generated
`/tmp/.hermes/config.yaml`, which is the file the gateway reads. They apply to
agents created after boot. A hand edit of that file is lost on restart because
`start.sh` regenerates it.

The session-reset knobs are applied by `executor.py`, not by Hermes. The pinned
Hermes ignores `session_reset:` in `config.yaml` (see `known-issues.md`). A
reset is never sent while another turn on the same chat is running, and the
first turn after a container start only starts the clock. They apply to the
plugin transport, which is the image default.

## Turn supervision

On the plugin transport a turn lasts until Hermes answers it. While any
message is running or waiting, the workspace reports busy: the heartbeat's
`active_tasks` and `is_busy`, which the idle digest, the scheduler and the
platform read before they send another autonomous wake.

The executor gives Hermes one message per chat at a time. A message that
arrives while a turn runs waits in the executor, in arrival order, and reaches
Hermes once the running turn is answered. Left to Hermes' own queue it would
run inside the running turn and be answered under that turn's id (hermes#393),
and Hermes' `/stop` would leave it queued to run next. A command a person sends
mid-turn that Hermes answers inline (`/stop`, `/new`, `/reset`, `/approve`,
`/deny`, `/status`, `/usage`, `/help`) skips the wait; the first three end the
turn that was running when the command was sent, and only that one.

These knobs bound a turn. Set them as workspace environment variables or
secrets; an invalid value is logged at WARNING and the default is used.

| Variable | Effect | Default |
|---|---|---|
| `HERMES_TURN_REPLY_DEADLINE_SECONDS` | Answer the caller of a turn that is still running (or still waiting) after this long, and let the turn go on. The clock starts when the message arrives and covers everything the turn sends Hermes, a scheduled or self-healing `/new` included. The platform stops waiting for an A2A answer at 30 minutes and then queues the request to be delivered again, which would run the work twice, so keep this below that. A channel plugin's turn (Telegram and the like) is answered by 540 s at the latest, under the channel's own 600 s. `0` turns it off. | `1500` |
| `HERMES_SELF_TURN_REPLY_DEADLINE_SECONDS` | The same for the runtime's own wakes whose senders give up within minutes and never show the reply: the delegation harvester (120 s), the idle digest (300 s), the lifecycle prompt (600 s). Other self-wakes keep the regular deadline: the platform's first-boot greeting shows the reply text and has its own 90 s fallback, and the scheduler's trigger lane waits for the real answer. `0` turns it off. | `60` |
| `HERMES_TURN_STALL_SECONDS` | Give up on the running turn after this long with no sign of life from the gateway: no reply, no other send for the chat, no tool call. A live run is never that quiet (see `HERMES_AGENT_NOTIFY_INTERVAL`), so this catches a crashed or wedged gateway. | the runtime's turn-lease TTL: `MOLECULE_TURN_LEASE_TTL_SECONDS`, else `A2A_COMPLETION_IDLE_TIMEOUT_SECONDS`, else `900` |
| `HERMES_TURN_SETTLE_SECONDS` | Settle the running turn when the gateway has been quiet this long but its health check still answers. A live run sends "still working" every `HERMES_AGENT_NOTIFY_INTERVAL`, so no run is going and nothing more will come under the turn's id: Hermes ended it without an answer (a bare silence marker such as `[SILENT]`, which Hermes drops on these turns), or answered it under another run's anchor (below). The caller gets that answer, or `(no response generated)`. Keep it above two notify intervals; `0` leaves such turns to the stall window. | `2 × HERMES_AGENT_NOTIFY_INTERVAL + 10` (`130` with `start.sh`), off when the notice is off |
| `HERMES_TURN_MAX_SECONDS` | Stop the running turn after it has run this long, however busy, by sending Hermes' `/stop`. The next waiting message goes to Hermes once `/stop` is answered; Hermes starts it when the stopped run lets go of the session, which waits for a tool call in flight to end. `0` turns the cap off. | the runtime's absolute turn cap: `MOLECULE_MAX_TURN_SECONDS`, else 4 × the stall default (`3600`) |
| `HERMES_TURN_QUEUE_MAX` | How many messages may wait behind the running turn on one chat. One more is refused with the runtime's own backpressure reply. | `32` |
| `HERMES_AGENT_NOTIFY_INTERVAL` | Seconds between the gateway's "still working" sends during a turn, the liveness signal inside one long tool call. Keep it well under the stall window; the executor warns at boot when it is not. | `60` (set by `start.sh`; upstream Hermes defaults to `180`) |
| `HERMES_TURN_POLL_SECONDS` | How often the executor checks the running turns and the tool-activity file | `1` |
| `HERMES_CONTROL_REPLY_SECONDS` | How long the executor waits for Hermes to answer `/new` or `/stop` | `120` |
| `HERMES_DROP_SELF_PINGS_WHEN_BUSY` | Fast-ack and drop a routine self-wake (idle digest, cron, scheduler, goal nudge, lifecycle wake) that arrives while a turn runs or waits, as the runtime's native executor does. Delegation results are never dropped: a Hermes turn has no other copy of them. `0` makes routine wakes wait their turn as well. | `1` |

What a caller sees:

- A turn still running at its reply deadline: the caller gets an interim
  reply. A person in the canvas sees `[hermes is still working on this after
  …s; its answer will appear in this chat when it is ready]`; a channel user is
  told the answer will be posted in the workspace's own chat; a peer (another
  agent's async delegation) is told the answer cannot be returned to its
  request; the runtime's own wake gets `[autonomous turn still running after
  …s; …]`, which the idle digest never shows in chat. The turn keeps running
  and the workspace stays busy.
- The answer that arrives after that is a late reply. A person's goes to the
  workspace's chat through `/notify`. An idle-digest wake's goes through the
  digest's reply forwarder, which drops `(idle)` and other housekeeping text.
  Any other late answer (a peer's turn, a scheduler tick, delegation results,
  a platform wake) is logged and not relayed: none of those senders shows the
  answer to a person, and the agent reports delegation results with its own
  tools.
- A turn the executor gave up on returns `[hermes plugin reply timeout: …]`,
  naming the stall window or the cap. When a person was already given the
  interim reply, that text goes to the chat instead.
- A message refused because `HERMES_TURN_QUEUE_MAX` messages are already
  waiting gets `{"status":"busy","retry":true}`.
- A routine self-wake dropped while busy is answered with
  `[autonomous wake skipped — …]`.

Runs Hermes starts itself. A final that arrives with no reply_to is the answer
of a run the executor did not send, such as the wake Hermes runs when a
`notify_on_complete` background job ends. Hermes marks every final `notify` and
every mid-run send `_interim_send`, so the executor tells such an answer from
progress. With nothing of ours running it goes to the chat through the idle
digest's forwarder. While one of our turns runs it is held: a message that
reached Hermes during such a run is answered under the run's anchor too, after
the run's own answer. The turn's own answer, or the settle window, decides;
whatever is not the turn's answer then goes to the chat. The pinned Hermes
refuses the background-job wake itself for molecule-a2a chats (`Watch
notification not admitted …: not_found`, hermes-platform-molecule-a2a#14), so
today an agent is not told that its background job ended.

Not covered here: another workspace's synchronous `delegate_task` cannot be
told apart from a person, and the runtime's client gives up after 300 s and
sends the task again with a new message id, which runs it a second time. Use
`delegate_task_async` for work that can take longer.

Each tool call Hermes makes is reported to the platform as an `agent_log`
activity row, the canvas tool chip. The `molecule_turn_activity` Hermes plugin
records the latest call in `MOLECULE_TOOL_ACTIVITY_FILE` and the executor posts
each record it sees; calls that finish within one poll interval of each other
are reported as one.
Those rows are also the activity the platform's stall watchdog needs from a
workspace that is busy for a long time: without them it probes, and then
restarts, a busy workspace that records no activity for about 17 minutes.

## Bundled Node.js

Hermes' package manager installs Node.js and npm into its tool store
(`~/.hermes/tools`); `scripts/hermes-post-install.sh` links their `node`,
`npm`, and `npx` into `/home/agent/.hermes/node/bin` at build time. When `node`
and `npx` are present there, `start.sh` prepends that directory to the
gateway's `PATH`. The image also
installs the same rule as `/etc/profile.d/hermes-node-path.sh`. The terminal
tool runs commands from a login-shell snapshot, where Debian's `/etc/profile`
replaces `PATH`, so this file is what puts Node.js on the terminal's `PATH`.

## State and continuity

The current Hermes home is `/tmp/.hermes`, so gateway-local state is not the
durable source of conversation continuity. `executor.py` forwards recent
platform history to seed a fresh Hermes session. Do not claim that a default
mount under `/home/agent/.hermes` preserves this image's live session store.

## Diagnostics

- Gateway log: `/tmp/.hermes/gateway.log`
- Molecule MCP log: `/tmp/.hermes/molecule-mcp-server.log`
- Gateway health: `http://127.0.0.1:8642/health`
- Molecule MCP endpoint: `http://127.0.0.1:9100/mcp`

These are container-internal diagnostics, not public platform endpoints.
