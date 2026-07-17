FROM python:3.11-slim

# System deps:
#   curl         — hermes installer + loopback health probe in start.sh
#   ca-certificates — TLS for all the outbound installs
#   git          — hermes installer clones the repo; also used by agent tools
#   gosu         — drop privileges in start.sh (single-process friendly)
#   xz-utils     — hermes installer extracts a Node 22 tarball (.tar.xz)
#   build-essential — some python deps in hermes `.[all]` extra compile from src
#
# T4 escalation leg (RFC internal#456 §9 / PR#474 — mirrors the
# already-live-verified claude-code template image, commit 12dd604):
#   sudo + util-linux(nsenter) + docker.io(CLI) are baked here so the
#   uid-1000 `agent` (see useradd below — UNCHANGED, agent stays
#   uid-1000; start.sh still `exec gosu agent`) has a wired, audited
#   path to host root inside the provisioner's `--privileged
#   --pid=host -v /:/host -v /var/run/docker.sock:/var/run/docker.sock`
#   container. Without sudo, a uid-1000 process in --privileged CANNOT
#   nsenter/chroot /host (--privileged grants caps to root, not
#   uid-1000) and cannot use the root:docker 0660 docker.sock — T4
#   would be provisioner-shape-only (the documented ABSENT-escalation
#   -leg gap). The sudoers drop-in + docker-group add are below, after
#   useradd, so `agent` exists. This is ADDITIVE: it does NOT change
#   the agent uid and does NOT change /configs token ownership (still
#   uid-1000, enforced by start.sh's `chown -R agent:agent /configs`
#   + the Layer-3 conformance gate). Hermes list_peers-401 class
#   (RFC internal#456 §10) must NOT regress.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git gosu xz-utils build-essential \
    sudo util-linux docker.io \
    && rm -rf /var/lib/apt/lists/*

# Non-root agent user — UNCHANGED. hermes-agent writes its state into
# ~/.hermes so mounting /home/agent as a persistent volume keeps skills
# + memory across workspace restarts. The agent runs as uid-1000; the
# T4 escalation leg below is additive and does NOT promote the agent to
# root. /configs/.auth_token must stay agent-owned (Hermes list_peers
# 401 class — RFC internal#456 §10).
RUN useradd -u 1000 -m -s /bin/bash agent

# --- T4 escalation leg (RFC internal#456 §9.3 / PR#474) ---
# Wired path: uid-1000 agent -> host root inside the provisioner's
# --privileged --pid=host -v /:/host -v docker.sock container.
#   1. NOPASSWD sudoers drop-in (mode 0440, visudo-validated at build
#      so a malformed sudoers can never ship a broken-sudo image).
#   2. agent in the `docker` group so the bind-mounted root:docker
#      0660 /var/run/docker.sock is usable without sudo.
# Atomic co-sequencing (RFC §10): this ships in the SAME image
# revision as the uid-1000 + agent-owned-token start.sh contract
# (PR#24 b682444); the Layer-3 conformance gate asserts BOTH on the
# running container. Mirrors claude-code template image (12dd604,
# already live-verified) verbatim.
RUN set -eux; \
    printf 'agent ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/agent-t4; \
    chmod 0440 /etc/sudoers.d/agent-t4; \
    visudo -cf /etc/sudoers.d/agent-t4; \
    groupadd -f docker; \
    groupadd -g 988 -f docker-host || true; \
    usermod -aG docker agent; \
    usermod -aG docker-host agent || true; \
    id agent

# --- Install molecule_runtime (bridge + A2A server) ---
# RUNTIME_VERSION is forwarded from this repository's publish-image
# workflow as a docker build-arg. Cascade-triggered builds set it to
# the exact version just published to the internal Gitea package registry.
# Including it as an ARG changes the cache key for the pip install layer below — without
# this, identical Dockerfile + identical requirements.txt would let
# docker reuse the cached layer with the previous version baked in
# (the cache trap that bit us 5x on 2026-04-27).
ARG RUNTIME_VERSION=

# Acquire the private runtime wheel from Gitea before resolving its public
# dependencies. Keeping the indexes in separate pip operations prevents a
# public package with the same name from competing with the canonical wheel.
ARG MOLECULE_RUNTIME_INDEX=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/

WORKDIR /app
COPY requirements.txt .
COPY scripts/prepare_runtime_requirements.py /usr/local/bin/prepare-runtime-requirements.py
RUN set -eu; \
    runtime_project="molecules-workspace-runtime"; \
    runtime_requirement="$(python3 /usr/local/bin/prepare-runtime-requirements.py \
      --requirements requirements.txt \
      --output /tmp/template-requirements.txt \
      --runtime-version "$RUNTIME_VERSION")"; \
    case "$runtime_requirement" in "$runtime_project"*) ;; *) exit 1 ;; esac; \
    rm -rf /tmp/molecule-runtime; \
    mkdir /tmp/molecule-runtime; \
    pip download --isolated --only-binary=:all: --no-deps \
      --index-url "$MOLECULE_RUNTIME_INDEX" \
      --dest /tmp/molecule-runtime "$runtime_requirement"; \
    wheel_count="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name '*.whl' | wc -l)"; \
    test "$wheel_count" -eq 1; \
    runtime_wheel="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name 'molecules_workspace_runtime-*.whl')"; \
    test -n "$runtime_wheel"; \
    pip install --isolated --no-cache-dir "$runtime_wheel" \
      -r /tmp/template-requirements.txt; \
    rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt

COPY adapter.py .
COPY __init__.py .
COPY executor.py .
COPY scripts/ /app/scripts/
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# Generic GIT_ASKPASS helper. Reads HTTPS Basic-Auth credentials from
# env vars (GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD, with GITEA_USER /
# GITEA_TOKEN as fallback) and emits them on the git credential-prompt
# protocol, so container-side `git` can authenticate to any private
# HTTPS remote without on-disk .gitconfig / .git-credentials mutation.
# Installed as /usr/local/bin/molecule-askpass — the platform-side
# provisioner sets GIT_ASKPASS to that path. Script body contains no
# hostnames or vendor literals; the deployer decides which remote the
# credentials apply to by virtue of populating those env vars.
COPY scripts/git-askpass.sh /usr/local/bin/molecule-askpass
RUN chmod +x /usr/local/bin/molecule-askpass

# --- Install the real Nous Research hermes-agent as the agent user ---
# The installer lives under the agent's home (~/.hermes, symlinks the
# `hermes` entrypoint into ~/.local/bin/). Running as root would place
# it in /root and break discovery.
#   --skip-setup → no interactive wizard (curl|bash is non-tty anyway
#                  but the installer treats this as "run anyway" by
#                  default; passing it explicitly avoids surprises).
USER agent
WORKDIR /home/agent
RUN curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
      | bash -s -- --skip-setup
# hermes installer symlinks ~/.hermes/hermes-agent/venv/bin/hermes into
# ~/.local/bin/hermes, so ~/.local/bin is the only PATH entry we need.
ENV PATH="/home/agent/.local/bin:${PATH}"

# --- Molecule A2A platform plugin (post-demo: native push parity) ---
# Two refs are installed into the same venv that the upstream installer
# created above:
#
#   1. The Molecule-maintained hermes-agent fork carrying the proposed
#      `register_platform_adapter` patch series (NousResearch/hermes-agent
#      PR #18775). Installed --force-reinstall over the upstream wheel so
#      `hermes_cli/plugins.py` exposes PluginContext.register_platform_adapter
#      and `gateway/run.py` honors plugin_platforms. Same deps as upstream
#      (the patch is pure-Python additions), so no resolver impact.
#   2. The Molecule A2A platform plugin itself, auto-discovered via
#      hermes's `hermes_agent.plugins` entry-point group.
#
# Until upstream PR #18775 merges, the fork is the only place the patch
# exists. Once merged + released, the fork install can be dropped and the
# plugin will load against the official wheel unchanged.
#
# The maintained fork lives at git.moleculesai.app/molecule-ai/hermes-agent.
ARG HERMES_FORK_REF=feat/platform-adapter-plugins
ARG HERMES_PLATFORM_MOLECULE_A2A_REF=24f4300a566eaa4af51e94c5e54f34af0978e508
# The hermes installer uses uv to create the venv and doesn't seed pip
# into it. Bootstrap pip first via ensurepip, then install both wheels.
RUN /home/agent/.hermes/hermes-agent/venv/bin/python3 -m ensurepip --upgrade && \
    /home/agent/.hermes/hermes-agent/venv/bin/python3 -m pip install --no-cache-dir --force-reinstall \
      "git+https://git.moleculesai.app/molecule-ai/hermes-agent.git@${HERMES_FORK_REF}#egg=hermes-agent" && \
    /home/agent/.hermes/hermes-agent/venv/bin/python3 -m pip install --no-cache-dir \
      "git+https://git.moleculesai.app/molecule-ai/hermes-platform-molecule-a2a.git@${HERMES_PLATFORM_MOLECULE_A2A_REF}#egg=hermes-platform-molecule-a2a"

# --- Pre-bake the management-MCP server (base-runtime helper; task #54) ---
# The kind=platform concierge launches `npx --prefer-offline @molecule-ai/mcp-server@<PIN>`
# in a HARD-deadline enumeration spawn at boot; without a warm cache it cold-pulls
# -> ETARGET / CF-WAF throttle -> #1027 "management MCP FAILED TO LAUNCH" fail-close
# (the launch-side of RCA #2970). The bake LOGIC + the pinned version live ONCE in
# the base runtime (molecule_runtime, pinned to the SDK contract
# management_mcp_server block) — this template just DELEGATES to it (ADR-004: SDK
# contract -> base-runtime default -> per-adapter override-if-needed; no per-template
# bake fork). hermes ships node under ~/.hermes/node/bin (not global), so we point
# the helper at it via MOLECULE_PREBAKE_NODE_BIN — the one sanctioned override. The
# helper's build-time OFFLINE self-check fails the image if the bake is broken.
RUN MOLECULE_PREBAKE_NODE_BIN=/home/agent/.hermes/node/bin \
    bash "$(python3 -c 'import molecule_runtime, os; print(os.path.dirname(molecule_runtime.__file__))')/scripts/prebake-mgmt-mcp.sh"

USER root
WORKDIR /app

ENV ADAPTER_MODULE=adapter \
    HERMES_API_BASE=http://127.0.0.1:8642/v1 \
    API_SERVER_ENABLED=true \
    API_SERVER_HOST=127.0.0.1 \
    API_SERVER_PORT=8642 \
    MOLECULE_A2A_PLATFORM_ENABLED=true \
    MOLECULE_A2A_PLATFORM_HOST=127.0.0.1 \
    MOLECULE_A2A_PLATFORM_PORT=8645 \
    MOLECULE_A2A_CALLBACK_HOST=127.0.0.1 \
    MOLECULE_A2A_CALLBACK_PORT=8646

# start.sh boots `hermes gateway` in the background, waits for :8642
# readiness, then exec's molecule-runtime on :8000.
ENTRYPOINT ["/usr/local/bin/start.sh"]
