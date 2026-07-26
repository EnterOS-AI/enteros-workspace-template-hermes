#!/usr/bin/env bash
# mcp-reconcile-watch.sh — hermes >= 0.19 discovers MCP servers EAGERLY at
# gateway startup, but the runtime's plugin adaptors append their
# mcp_servers stanzas (molecule-platform, molecule-self) to config.yaml
# AFTER the gateway launches. Without reconciliation the agent silently
# boots with only the base 'molecule' server (31 tools instead of ~91 —
# no schedule tools, no org-management surface; 2026-07-23 concierge
# "I don't have scheduling"). Nothing fails, so nothing logs.
#
# This watcher polls the PARSED mcp_servers mapping (a STRUCTURAL compare,
# not a raw-byte one — formatting / quoting / key-order churn from an
# idempotent yaml rewrite is invisible; only a real server add / remove /
# spec-change counts, RFC rule 5); when a plugin adaptor changes
# it, waits for the writes to settle, re-stamps the clean-shutdown marker
# (so the restarted gateway resumes the boot session instead of
# suspending it), gracefully drains the old gateway (SIGTERM lets an
# in-flight turn — the first-boot greeting, an early user message —
# finish and reply; SIGKILL only after the drain window), relaunches, and
# health-checks the replacement with one retry. It stays live for the
# whole watch window and restarts on EACH settled change (bounded by
# MCPWATCH_MAX_RESTARTS) so a slow second adaptor still gets picked up.
# (Review wf_7cb5003d findings #4/#8/#9.)
#
# Parameterized so scripts/test-mcp-reconcile-watch.sh can drive it
# against a fake gateway hermetically. Production values are supplied by
# start.sh. Required env:
#   MCPWATCH_CONFIG        config.yaml path to watch
#   MCPWATCH_GATEWAY_PID   pid of the currently-running gateway
#   MCPWATCH_LAUNCH_CMD    shell command that relaunches the gateway
#                          (backgrounded by THIS script; must exec the
#                          long-running process so $! is the gateway pid)
#   MCPWATCH_HEALTH_URL    URL that answers 200 when the gateway is up
#   MCPWATCH_LOG_FILE      log file the relaunched gateway appends to
#   MCPWATCH_MARKER        clean-shutdown marker path ("" disables)
# Tunables (defaults = production):
#   MCPWATCH_POLL_SECS (5) MCPWATCH_SETTLE_SECS (5) MCPWATCH_TICKS (60)
#   MCPWATCH_DRAIN_SECS (90) MCPWATCH_HEALTH_TICKS (60)
#   MCPWATCH_MAX_RESTARTS (3) MCPWATCH_MARKER_OWNER (agent)
set -uo pipefail

: "${MCPWATCH_CONFIG:?}" "${MCPWATCH_GATEWAY_PID:?}" "${MCPWATCH_LAUNCH_CMD:?}"
: "${MCPWATCH_HEALTH_URL:?}" "${MCPWATCH_LOG_FILE:?}"
MCPWATCH_MARKER="${MCPWATCH_MARKER:-}"
MCPWATCH_POLL_SECS="${MCPWATCH_POLL_SECS:-5}"
MCPWATCH_SETTLE_SECS="${MCPWATCH_SETTLE_SECS:-5}"
MCPWATCH_TICKS="${MCPWATCH_TICKS:-60}"
MCPWATCH_DRAIN_SECS="${MCPWATCH_DRAIN_SECS:-90}"
MCPWATCH_HEALTH_TICKS="${MCPWATCH_HEALTH_TICKS:-60}"
MCPWATCH_MAX_RESTARTS="${MCPWATCH_MAX_RESTARTS:-3}"
MCPWATCH_MARKER_OWNER="${MCPWATCH_MARKER_OWNER:-agent}"

# Zombie-aware liveness (review wf_3a7b849d #2). The old inline watcher used
# process_is_running (scripts/process-liveness.sh), which reads /proc State and
# excludes Z (zombie) and X (dead). A bare `kill -0` returns TRUE for a zombie —
# and the first gateway's zombie is never reaped (start.sh exec'd into
# molecule-runtime, which reaps no inherited children), so a `kill -0` drain
# loop would burn the whole 90s window against a long-dead process and then log
# a spurious SIGKILL. Read the proc State directly (self-contained — the baked
# watcher must not depend on sourcing another script). Unreadable status ->
# treat as gone (the process left /proc). This also fixes the EPERM
# conflation: `kill -0` on a live process we can't signal returns success; here
# a live process's State is readable and non-Z regardless of signal perms.
mcpwatch_pid_running() {
  local pid=$1 state
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  state=$(awk '$1 == "State:" { print $2; exit }' "/proc/${pid}/status" 2>/dev/null) || return 1
  [ -n "$state" ] && [ "$state" != "Z" ] && [ "$state" != "X" ]
}

# Structural fingerprint of the PARSED mcp_servers mapping (RFC rule 5).
# The old raw-byte hash (a sed of the /^mcp_servers:/ block piped to md5sum)
# fired on any COSMETIC delta — safe_dump re-quoting a url, reordering keys,
# whitespace — which is exactly what an idempotent runtime rewrite produces,
# so the watcher restarted the gateway (killing the concierge greeting) for a
# semantically-unchanged config. Parse the yaml and hash a canonical
# (sorted-key, whitespace-free) json of ONLY mcp_servers instead: formatting /
# quoting / key-order churn is invisible, but a genuine server add / remove /
# spec-change still moves the fingerprint and triggers the reconcile restart.
# Unreadable / malformed / pyyaml-less -> a stable empty fingerprint (fail-safe:
# never a spurious restart; python3 + pyyaml are always present in the runtime
# image — molecule_runtime imports yaml).
mcp_block_hash() {
  python3 -c '
import sys, json, hashlib
try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    servers = data.get("mcp_servers") if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        servers = {}
    canon = json.dumps(servers, sort_keys=True, separators=(",", ":"))
except Exception:
    canon = ""
sys.stdout.write(hashlib.md5(canon.encode("utf-8")).hexdigest())
' "$MCPWATCH_CONFIG" 2>/dev/null
}

restart_gateway_for_mcp() {
  echo "[mcp-reconcile] mcp_servers changed post-launch (plugin adaptors) — restarting hermes gateway to pick them up (0.19 eager discovery)"
  if [ -n "$MCPWATCH_MARKER" ]; then
    install -o "$MCPWATCH_MARKER_OWNER" -g "$MCPWATCH_MARKER_OWNER" /dev/null "$MCPWATCH_MARKER" 2>/dev/null \
      || : > "$MCPWATCH_MARKER"
  fi
  kill "$CURRENT_GW_PID" 2>/dev/null || true
  for _ in $(seq 1 "$MCPWATCH_DRAIN_SECS"); do
    mcpwatch_pid_running "$CURRENT_GW_PID" || break
    sleep 1
  done
  if mcpwatch_pid_running "$CURRENT_GW_PID"; then
    echo "[mcp-reconcile] gateway did not drain within ${MCPWATCH_DRAIN_SECS}s — SIGKILL (in-flight turn lost)" >&2
    kill -9 "$CURRENT_GW_PID" 2>/dev/null || true
    sleep 2
  fi
  for attempt in 1 2; do
    bash -c "$MCPWATCH_LAUNCH_CMD" >>"$MCPWATCH_LOG_FILE" 2>&1 &
    CURRENT_GW_PID=$!
    for _ in $(seq 1 "$MCPWATCH_HEALTH_TICKS"); do
      curl -fsS "$MCPWATCH_HEALTH_URL" >/dev/null 2>&1 && break
      mcpwatch_pid_running "$CURRENT_GW_PID" || break
      sleep 1
    done
    if curl -fsS "$MCPWATCH_HEALTH_URL" >/dev/null 2>&1; then
      echo "[mcp-reconcile] hermes gateway restarted (pid $CURRENT_GW_PID) with reconciled mcp_servers"
      return 0
    fi
    echo "[mcp-reconcile] restarted gateway failed health (attempt $attempt) — retrying" >&2
    kill -9 "$CURRENT_GW_PID" 2>/dev/null || true
    sleep 2
  done
  echo "[mcp-reconcile] gateway did not come back healthy after mcp reconcile — see $MCPWATCH_LOG_FILE" >&2
  return 1
}

CURRENT_GW_PID="$MCPWATCH_GATEWAY_PID"
BASELINE=$(mcp_block_hash)
RESTARTS=0
for _ in $(seq 1 "$MCPWATCH_TICKS"); do
  sleep "$MCPWATCH_POLL_SECS"
  CUR=$(mcp_block_hash)
  if [ "$CUR" != "$BASELINE" ]; then
    # Settle: adaptors may write several stanzas back-to-back.
    while :; do
      sleep "$MCPWATCH_SETTLE_SECS"
      NEXT=$(mcp_block_hash)
      [ "$NEXT" = "$CUR" ] && break
      CUR=$NEXT
    done
    # Only restart if the SETTLED block genuinely DIFFERS from the baseline.
    # An idempotent, non-atomic rewrite (e.g. the real runtime re-writing the
    # config the `molecule-runtime-prepare` pre-step already materialized, or
    # a mid-write truncate/append flicker) transiently changes the hash then
    # settles back to identical content. Restarting on that would be a
    # needless ~gateway-restart outage — and with pre-materialization the
    # steady state is EXACTLY this idempotent rewrite, so this guard is what
    # keeps the watcher dormant on a healthy boot.
    if [ "$CUR" != "$BASELINE" ]; then
      restart_gateway_for_mcp || break
      RESTARTS=$((RESTARTS + 1))
      [ "$RESTARTS" -ge "$MCPWATCH_MAX_RESTARTS" ] && break
    fi
    # Re-baseline to the block hash READ NOW (review wf_3a7b849d #3), not the
    # pre-restart settled $CUR. The restart window is long (up to ~150s: drain
    # + health polls), and any adaptor write that lands during it is ALREADY
    # read by the relaunched gateway's eager discovery — so baselining to $CUR
    # would see that write as a fresh diff on the next poll and fire a second,
    # gratuitous restart (killing the just-healthy gateway and burning a
    # MAX_RESTARTS slot). Re-hashing here captures the current on-disk truth in
    # both the restarted and the dormant idempotent-rewrite cases.
    BASELINE=$(mcp_block_hash)
  fi
done
