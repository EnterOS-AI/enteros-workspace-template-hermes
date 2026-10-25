# shellcheck shell=sh
# Put the Node.js that the hermes install ships on PATH. Sourced, never run.
#
# hermes' package manager installs node and npm into its tool store; the image
# build links node/npm/npx into /home/agent/.hermes/node/bin
# (scripts/hermes-post-install.sh). hermes finds its own Node through the
# package manager, but the agent's shell commands only see PATH.
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
