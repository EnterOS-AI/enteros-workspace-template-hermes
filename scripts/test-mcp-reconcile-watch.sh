#!/usr/bin/env bash
# test-mcp-reconcile-watch.sh — hermetic unit test for
# scripts/mcp-reconcile-watch.sh (the hermes-0.19 eager-MCP reconciler).
#
# Drives the REAL watcher against a FAKE gateway: a python http server
# standing in for hermes's /health, launched by the same MCPWATCH_LAUNCH_CMD
# contract start.sh uses. Asserts the three behaviors the 2026-07-23
# incident + review wf_7cb5003d demanded:
#   1. an mcp_servers change (post-settle) restarts the gateway exactly once
#   2. the clean-shutdown marker is re-stamped BEFORE the kill
#   3. a SECOND change after the first restart triggers another restart
#      (the watcher is not one-shot)
# No hermes install, no docker — runs in the Shell-unit-tests CI job.
set -euo pipefail

WORK=$(mktemp -d)
FAKE_PORT=$(python3 - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PY
)
cleanup() {
  pkill -f "fake_gateway_${FAKE_PORT}" 2>/dev/null || true
  [ -n "${WATCH_PID:-}" ] && kill "$WATCH_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$WORK/fake_gateway.py" <<PY
import http.server, sys, os
# argv[1]=port argv[2]=launch-count file — each boot appends its pid so the
# test can count restarts and address the CURRENT process.
port = int(sys.argv[1])
with open(sys.argv[2], "a") as f:
    f.write(str(os.getpid()) + "\n")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PY

# mcp_servers LAST so appended stanzas extend the block — modeling the
# runtime adaptors, which rewrite the whole yaml and keep the block
# contiguous (the watcher hashes /^mcp_servers:/ through the next
# top-level key).
CONFIG="$WORK/config.yaml"
cat > "$CONFIG" <<EOF
model:
  default: test
platforms:
  molecule-a2a:
    enabled: true
mcp_servers:
  molecule:
    url: http://127.0.0.1:9100/mcp
EOF

BOOTS="$WORK/boots.txt"
LAUNCH="exec python3 $WORK/fake_gateway.py $FAKE_PORT $BOOTS # fake_gateway_${FAKE_PORT}"

# Boot #1 (stands in for start.sh's original gateway launch).
bash -c "$LAUNCH" >>"$WORK/gw.log" 2>&1 &
GW1=$!
for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$FAKE_PORT/health" >/dev/null 2>&1 && break
  sleep 0.2
done
curl -fsS "http://127.0.0.1:$FAKE_PORT/health" >/dev/null

MARKER="$WORK/.clean_shutdown"
MCPWATCH_CONFIG="$CONFIG" \
MCPWATCH_GATEWAY_PID="$GW1" \
MCPWATCH_LAUNCH_CMD="$LAUNCH" \
MCPWATCH_HEALTH_URL="http://127.0.0.1:$FAKE_PORT/health" \
MCPWATCH_LOG_FILE="$WORK/gw.log" \
MCPWATCH_MARKER="$MARKER" \
MCPWATCH_MARKER_OWNER="$(id -un)" \
MCPWATCH_POLL_SECS=1 \
MCPWATCH_SETTLE_SECS=1 \
MCPWATCH_TICKS=40 \
MCPWATCH_DRAIN_SECS=10 \
MCPWATCH_HEALTH_TICKS=20 \
MCPWATCH_MAX_RESTARTS=3 \
  bash "$(dirname "$0")/mcp-reconcile-watch.sh" >"$WORK/watch.log" 2>&1 &
WATCH_PID=$!

sleep 3  # let the watcher take its baseline

# --- Change 0: an IDEMPOTENT rewrite (identical content) must NOT restart ---
# This is the steady state once `molecule-runtime-prepare` pre-materializes the
# config before launch: the real runtime later re-writes the SAME mcp_servers
# block, and the watcher must stay dormant (a restart here would re-introduce
# the very ~90s outage pre-materialization eliminates). Rewrite byte-identical
# content and confirm no restart across several poll cycles.
cp "$CONFIG" "$WORK/config.rewrite" && cat "$WORK/config.rewrite" > "$CONFIG"
sleep 5
if [ "$(wc -l < "$BOOTS")" -ne 1 ]; then
  echo "FAIL: idempotent config rewrite triggered a needless gateway restart (boots=$(wc -l < "$BOOTS"))"
  cat "$WORK/watch.log"; exit 1
fi
echo "OK: idempotent rewrite did NOT restart the gateway (watcher dormant)"

# --- Change 1: adaptor appends a second MCP server ---
cat >> "$CONFIG" <<EOF
  molecule-self:
    command: sh
EOF

for _ in $(seq 1 60); do
  [ "$(wc -l < "$BOOTS" 2>/dev/null || echo 0)" -ge 2 ] && break
  sleep 0.5
done
BOOT_COUNT=$(wc -l < "$BOOTS")
if [ "$BOOT_COUNT" -lt 2 ]; then
  echo "FAIL: watcher did not restart the gateway after an mcp_servers change"
  cat "$WORK/watch.log"; exit 1
fi
echo "OK: change 1 restarted the gateway (boots=$BOOT_COUNT)"

if [ ! -f "$MARKER" ]; then
  echo "FAIL: clean-shutdown marker was not re-stamped before the restart"
  cat "$WORK/watch.log"; exit 1
fi
echo "OK: clean-shutdown marker re-stamped"

OLD_GW1_DEAD=1
kill -0 "$GW1" 2>/dev/null && OLD_GW1_DEAD=0
if [ "$OLD_GW1_DEAD" -ne 1 ]; then
  echo "FAIL: original gateway pid $GW1 still running after reconcile restart"
  exit 1
fi
echo "OK: original gateway was terminated"

curl -fsS "http://127.0.0.1:$FAKE_PORT/health" >/dev/null || {
  echo "FAIL: relaunched gateway not healthy"; cat "$WORK/watch.log"; exit 1; }
echo "OK: relaunched gateway healthy"

# --- Change 2: a SLOW second adaptor writes after the first restart ---
sleep 2
cat >> "$CONFIG" <<EOF
  molecule-platform:
    command: sh
EOF

for _ in $(seq 1 60); do
  [ "$(wc -l < "$BOOTS")" -ge 3 ] && break
  sleep 0.5
done
if [ "$(wc -l < "$BOOTS")" -lt 3 ]; then
  echo "FAIL: watcher is one-shot — second adaptor change was not reconciled"
  cat "$WORK/watch.log"; exit 1
fi
echo "OK: change 2 (slow second adaptor) restarted the gateway again"

echo
echo "✓ mcp-reconcile-watch.sh: restart-on-change, marker re-stamp, and multi-restart all verified"
