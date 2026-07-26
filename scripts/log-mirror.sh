#!/usr/bin/env bash

# log-mirror.sh — mirror a file-only log to the container's stdout (PID1) so
# `docker logs` / Dozzle can see it.
#
# WHY: the hermes gateway and the Molecule A2A MCP server are launched DETACHED
# (`nohup gosu agent ... >>"$LOG" 2>&1 &`) with their output redirected to FILES
# under HERMES_HOME (gateway.log, molecule-mcp-server.log) rather than to the
# container's stdout. Docker's json-file logging driver — and therefore Dozzle,
# which just renders that stream — only captures PID1's stdout/stderr, so those
# file-only logs never reach `docker logs` / Dozzle. "Wire every log to Dozzle"
# requires them on PID1 stdout.
#
# HOW: attach one background `tail -n0 -F` follower per log that appends new
# lines to /proc/1/fd/1 (PID1's stdout == the container's json-file stream).
# This is the LEAST-INVASIVE option:
#   * It does NOT touch the producers' own `>>"$LOG" 2>&1` redirection, so every
#     FILE reader is unaffected — the reconcile watcher (which polls config.yaml,
#     NOT the log), the boot `tail -40/-80 "$LOG"` error dumps, and any operator
#     `cat`-ing the file all keep working exactly as before.
#   * `-F` (capital) follows across truncate/rotate AND keeps mirroring after the
#     reconcile watcher restarts the gateway — the relaunch re-appends to the
#     SAME file (mcp-reconcile-watch.sh: `>>"$MCPWATCH_LOG_FILE"`), so the single
#     follower started at boot never needs re-attaching.
#
# SAFETY: guarded so it can NEVER abort boot under `set -euo pipefail`:
#   * If /proc/1/fd/1 is not writable (unusual sandbox), skip silently — the log
#     stays file-only, boot proceeds.
#   * A duplicate follower for the same log is not spawned (idempotent), so a
#     defensive re-invocation can't stack tailers. (start.sh is the PID1
#     entrypoint and runs once per boot, so this is belt-and-braces.)
#   * The follower's own stderr is discarded; the write redirect uses `>>`
#     (append) so it can never truncate a regular-file stdout target.
#
# The follower is a plain background child of the PID1 shell. When start.sh
# finally `exec`s into molecule-runtime the PID-1 image is replaced but the
# follower keeps running (exec does not signal children) and /proc/1/fd/1 still
# resolves to the same container stdout stream — so mirroring continues for the
# whole container lifetime and is torn down automatically when the container
# exits.

# mirror_log_to_stdout <logfile>
# Start (at most one) background `tail -n0 -F` that mirrors <logfile> to PID1
# stdout. Always returns 0; prints a one-line status.
mirror_log_to_stdout() {
  local log="${1:?mirror_log_to_stdout: log path required}"
  # PID1 stdout == the container's docker json-file stream (what Dozzle shows).
  # Overridable ONLY so scripts/test-log-mirror.sh can point the mirror at a
  # temp file on a host where /proc/1/fd/1 isn't the container stdout; runtime
  # always takes the default. (Mirrors process-liveness.sh's optional proc root.)
  local target="${MIRROR_STDOUT_TARGET:-/proc/1/fd/1}"

  # If we can't open the target for append, skip — never let this kill boot.
  if ! { : >>"$target"; } 2>/dev/null; then
    echo "[log-mirror] ${target} not writable; ${log} stays file-only (not on Dozzle)" >&2
    return 0
  fi

  # Idempotent: don't stack a second follower on the same log.
  if command -v pgrep >/dev/null 2>&1 \
     && pgrep -f "tail -n0 -F ${log}\$" >/dev/null 2>&1; then
    echo "[log-mirror] stdout mirror already running for ${log}; not duplicating"
    return 0
  fi

  # -n0: start from the current end (don't re-dump pre-existing content; on a
  #      fresh boot the file was just installed empty anyway).
  # -F : follow across rotation AND across the reconcile watcher's gateway
  #      restart (same file re-appended).
  tail -n0 -F "$log" >>"$target" 2>/dev/null &
  echo "[log-mirror] mirroring ${log} -> container stdout (tail pid $!) for Dozzle/docker logs"
  return 0
}
