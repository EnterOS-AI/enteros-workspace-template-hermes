#!/usr/bin/env bash

# Signal probes require CAP_KILL when PID 1 is root and the child has dropped
# to uid 1000. Local workspaces intentionally omit that capability, so read the
# kernel's process state instead. The optional proc root keeps this testable on
# non-Linux development hosts.
process_is_running() {
  local pid=${1:-}
  local proc_root=${2:-/proc}
  local state

  case "${pid}" in
    ''|*[!0-9]*) return 1 ;;
  esac

  state=$(awk '$1 == "State:" { print $2; exit }' \
    "${proc_root}/${pid}/status" 2>/dev/null) || return 1

  [ -n "${state}" ] && [ "${state}" != "Z" ] && [ "${state}" != "X" ]
}
