#!/usr/bin/env bash
# mcp-reconcile-watch.sh — hermes >= 0.19 discovers MCP servers EAGERLY at
# gateway startup, but the runtime's plugin adaptors append their
# mcp_servers stanzas (molecule-platform, molecule-self) to config.yaml
# AFTER the gateway launches. Without reconciliation the agent silently
# boots with only the base 'molecule' server (31 tools instead of ~91 —
# no schedule tools, no org-management surface; 2026-07-23 concierge
# "I don't have scheduling"). Nothing fails, so nothing logs.
#
# This watcher polls the mcp_servers block; when a plugin adaptor changes
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

mcpwatch_pid_running() {
  kill -0 "$1" 2>/dev/null
}

mcp_block_hash() {
  sed -n '/^mcp_servers:/,/^[a-z_]/p' "$MCPWATCH_CONFIG" 2>/dev/null | md5sum | cut -d' ' -f1
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
    restart_gateway_for_mcp || break
    BASELINE=$(mcp_block_hash)
    RESTARTS=$((RESTARTS + 1))
    [ "$RESTARTS" -ge "$MCPWATCH_MAX_RESTARTS" ] && break
  fi
done
