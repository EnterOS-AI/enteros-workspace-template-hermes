# shellcheck shell=sh
# Put the Node.js that the hermes installer bundles on PATH. Sourced, never run.
#
# The installer unpacks node/npm/npx under the BUILD-time hermes home,
# /home/agent/.hermes/node/bin. At runtime HERMES_HOME is /tmp/.hermes, so
# hermes' own managed-node lookup ($HERMES_HOME/node/bin) misses it.
#
# Two consumers, one rule:
#   * start.sh sources it to build the PATH it gives the hermes gateway.
#   * The image installs it as /etc/profile.d/hermes-node-path.sh. hermes'
#     terminal tool runs every command from a `bash -l` environment snapshot,
#     and Debian's /etc/profile overwrites PATH in a login shell before it
#     sources /etc/profile.d/*.sh, so this is the only place where an entry
#     reaches the agent's commands.
#
# Prepends only when node AND npx are executable there, and only once.
# HERMES_BUNDLED_NODE_BIN overrides the directory (tests).
_hermes_node_bin="${HERMES_BUNDLED_NODE_BIN:-/home/agent/.hermes/node/bin}"
if [ -x "${_hermes_node_bin}/node" ] && [ -x "${_hermes_node_bin}/npx" ]; then
  case ":${PATH}:" in
    *":${_hermes_node_bin}:"*) ;;
    *) PATH="${_hermes_node_bin}${PATH:+:${PATH}}"; export PATH ;;
  esac
fi
unset _hermes_node_bin
