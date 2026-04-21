# Known Issues — hermes Workspace Template

This document tracks unresolved and partially-resolved issues that are known to
occur when running the hermes workspace template. Each entry includes the symptom,
affected versions, workaround, and (where applicable) a tracker reference.

---

## 1. Hermes Version Mismatch Causes Event Loop to Exit Prematurely

**Severity:** High
**Affects:** Template versions that pin `hermes` without an upper bound, combined with
platform runtime environments that bundle an older `hermes` version.

**Symptom:**
The adapter starts, prints the startup banner, then exits immediately with exit code
0 and no error message:

```
[hermes] INFO — event loop starting (runtime=hermes version=0.8.2)
[hermes] INFO — subscribed to platform event stream
[hermes] INFO — event loop stopped
```

No tasks are processed.

**Root cause:**
The `AgentLoop.run()` coroutine in hermes `0.8.x` changed its shutdown handshake
sequence. The platform's event dispatcher still expects the old handshake
(`await loop.join(timeout=5)`), causing it to send a `STOP` signal before any tasks
are dispatched. The adapter interprets `STOP` as a graceful shutdown and exits.

**Workaround:**
Pin hermes to the exact version bundled with the target platform release:

```bash
# Determine the platform's bundled hermes version
curl -s https://platform.molecule.ai/api/v1/workspaces/{id}/runtime-info \
  -H "Authorization: Bearer $MOLECULE_TOKEN" | jq '.hermes_version'
```

Update `requirements.txt`:

```
hermes==0.7.3    # whatever the platform reports
```

**Fix:** Tracked in internal ticket MOL-5102. A future platform release will
backport the handshake fix to `0.7.x` so older adapters work with the current
platform.

---

## 2. `adapter.py` Not Forwarding HEARTBEAT Events

**Severity:** Medium
**Affects:** Template versions `v0.7.x` when `observability.heartbeat_interval_seconds`
is set to a value greater than 120 seconds.

**Symptom:**
The Molecule platform dashboard shows the workspace as "inactive" after the first
task completes, even though the adapter is still running and polling. No heartbeat
events appear in the platform's event log.

**Root cause:**
A logic error in `adapter.py` prevents the heartbeat scheduling task from starting
when the interval exceeds 120 seconds. The condition:

```python
if heartbeat_interval > 120:   # BUG: should be >=
    logger.info("heartbeat disabled: interval too large")
    return
```

should be `> 120` (correct) but was written as a no-op for the > case only.
However, the scheduler thread was never started for any interval due to an
additional indentation error in the task creation block.

**Workaround:**
Set `heartbeat_interval_seconds` to 60 or lower in `config.yaml`:

```yaml
observability:
  heartbeat_interval_seconds: 60   # must be <= 120
```

**Fix:** Patch applied in template v0.7.2. Upgrade to v0.7.2+ to resolve, or apply
the following diff to `adapter.py`:

```diff
-        if heartbeat_interval > 120:
+        if heartbeat_interval > 120:   # warn only; do not skip
             logger.info("heartbeat disabled: interval too large")
-            return
+        heartbeat_task = asyncio.create_task(_heartbeat_loop(heartbeat_interval))
+        self._heartbeat_task = heartbeat_task
```

---

## 3. `system-prompt.md` Truncated at Token Limit

**Severity:** Medium
**Affects:** All hermes template versions when `system-prompt.md` exceeds approximately
32,000 tokens.

**Symptom:**
The agent behaves as if only part of the system prompt was applied. Guardrails
described in the second half of `system-prompt.md` are ignored, and the agent produces
outputs that should have been blocked.

The adapter log shows:

```
[hermes.model] WARNING — system prompt exceeds max_tokens, truncating to 8192 tokens
[hermes.model] DEBUG  — system prompt loaded from system-prompt.md (47823 tokens)
```

**Root cause:**
The hermes adapter's system prompt loader reads the entire `system-prompt.md` into
memory and passes it directly as the `system` parameter to the LLM API call. If the
total token count (system + conversation + max_tokens) exceeds the model's context
limit, the API either errors or silently truncates. The adapter currently does not
perform client-side truncation before sending.

**Workaround:**
Keep `system-prompt.md` under 8,000 tokens (~32,000 characters). Review it with:

```bash
# Count tokens using the anthropic tokenizer (requires pip install tiktoken)
python -c "
import tiktoken
enc = tiktoken.get_encoding('cl100k_base')
with open('system-prompt.md') as f:
    tokens = len(enc.encode(f.read()))
print(f'system-prompt.md: {tokens} tokens')
"
```

If over the limit, split content into separate files and load conditionally via the
adapter's bootstrap hook.

**Fix:** Planned for template v0.9.0. The adapter will perform client-side
truncation using `tiktoken` before sending, preserving the last 20% of the prompt
as a safety net (tracked in ticket MOL-5231).

---

## 4. `config.yaml` Model Override Not Respected

**Severity:** Low
**Affects:** Template v0.7.x when the platform injects a workspace-level model
override (e.g. forcing all workspaces to use `claude-3-5-haiku` for cost control).

**Symptom:**
The adapter uses the model specified in the local `config.yaml` instead of the model
forced by the platform. This causes a mismatch between the platform's billing
dashboard and actual usage, and may cause the platform to reject task responses if
the platform-forced model has a different API key scope.

**Root cause:**
The adapter loads `config.yaml` first, then overlays platform-provided config
patches. However, the overlay logic processes keys in alphabetical order, and the
platform's `model.name` patch arrives before the local `model.name` is read. Because
both are at the same YAML depth, the local value overwrites the platform patch
instead of the other way around.

**Workaround:**
Remove `model` from your local `config.yaml` and rely entirely on the platform
override. If local overrides are required for development, use environment variable
substitution in a separate dev config:

```bash
# In .env.local
HERMES_MODEL_NAME=claude-3-5-haiku
```

```yaml
# config.dev.yaml (do NOT commit this)
model:
  name: "${HERMES_MODEL_NAME}"
```

Run with:

```bash
python adapter.py --config config.yaml --config-override config.dev.yaml
```

**Fix:** The adapter's config merge logic will be updated in v0.8.0 to respect a
`priority: platform` field in `config.yaml` so platform overrides always win
(tracked in ticket MOL-5340).
