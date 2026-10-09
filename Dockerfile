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

# --- GitHub CLI (issue #371) ---
# `gh` is a RUNTIME DEPENDENCY of this image, not a convenience. The
# seo-agent workspace class is built entirely on it: its SETUP.md runs
# `gh auth refresh` / `gh auth token`, its agent-policies call `gh api`,
# and the tenant's scheduled tick prompt is written around
# `gh pr list` / `gh pr create` / `gh pr merge`. Without it the agent
# cannot produce its primary output (shipping PRs to a Next.js site) —
# confirmed live on workspace 90139d37, where `gh pr list` returned
# "command not found" and an 11-minute tick left zero git side effects.
#
# It fails SILENTLY: the `command not found` is swallowed inside the
# agent turn, so the workspace stays status=online / wedged=false /
# error_rate=0 and the operator-visible symptom is the agent reporting
# "queue empty" — which reads as no work available, not no tooling.
#
# Installed from the official cli.github.com apt repo (keyring + source
# list), in its own layer so the base package layer above stays cached.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/* && \
    gh --version

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
COPY scripts/mcp-reconcile-watch.sh /usr/local/bin/mcp-reconcile-watch.sh
RUN chmod +x /usr/local/bin/start.sh /usr/local/bin/mcp-reconcile-watch.sh

# Generic GIT_ASKPASS helper. Reads HTTPS Basic-Auth credentials from
# env vars (GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD, with GITEA_USER /
# GITEA_TOKEN as fallback) and emits them on the git credential-prompt
# protocol, so container-side `git` can authenticate to any private
# HTTPS remote without on-disk .gitconfig / .git-credentials mutation.
# Installed as /usr/local/bin/molecule-askpass — the platform-side
# provisioner sets GIT_ASKPASS to that path. Script body contains no
# hostnames or vendor literals; the deployer decides which remote the
# credentials apply to by virtue of populating those env vars.
COPY scripts/molecule-askpass /usr/local/bin/molecule-askpass
RUN chmod +x /usr/local/bin/molecule-askpass

# --- Install the real Nous Research hermes-agent as the agent user ---
# The installer lives under the agent's home (~/.hermes, symlinks the
# `hermes` entrypoint into ~/.local/bin/). Running as root would place
# it in /root and break discovery.
#   --skip-setup → no interactive wizard (a build step has no tty anyway
#                  but the installer treats this as "run anyway" by
#                  default; passing it explicitly avoids surprises).
#   --commit SHA → upstream's own first-class checkout pin (install.sh
#                  `--commit`, validated as a hex SHA then `git checkout
#                  --detach`). See HERMES_COMMIT below for WHY this is the
#                  pin that matters.
#   --force-commit → defensive, and worth understanding. install.sh SUPPRESSES
#                  a `--commit` that would move an EXISTING install backwards
#                  (so a stale desktop bootstrap binary cannot rewind a
#                  current checkout): if the pin is an ancestor of HEAD it
#                  logs "Ignoring --commit ...: the checkout is already
#                  newer" and EXITS 0. Our pin IS an ancestor of main, so
#                  that is the branch to worry about — an ignored pin looks
#                  exactly like a successful one from the outside.
#                  Observed on a clean build of this Dockerfile: the
#                  installer takes the plain "Pinning checkout to commit ..."
#                  path and reports "HEAD is now at 29112bef0 chore: release
#                  v0.21.0", i.e. the suppressing branch is not reached and
#                  --force-commit changes nothing there. It is passed anyway
#                  because that outcome depends on clone shape and on whether
#                  a checkout already exists, neither of which this Dockerfile
#                  should have to reason about. The `rev-parse` assertion
#                  below is what actually makes the pin trustworthy: it fails
#                  the build if the checkout is not at HERMES_COMMIT, whatever
#                  path the installer took to get there.
#
# THE PIN THAT ACTUALLY DECIDES WHAT RUNS (2026-09-03)
# ----------------------------------------------------
# ~/.local/bin/hermes is NOT the venv console script. It is a wrapper that
# clears PYTHONPATH/PYTHONHOME and execs
#   $HERMES_ROOT/venv/bin/python $HERMES_ROOT/hermes "$@"
# Running a script at $HERMES_ROOT puts $HERMES_ROOT at sys.path[0], AHEAD of
# site-packages. So the gateway imports the git CHECKOUT, not the wheel.
# Confirmed live on enteros-minori / enteros-ws-c7937b219232: /proc/138/cmdline
# is exactly that argv pair. scripts/neutralize-vendor-branding.py documents
# the same finding from the other end (it must patch the checkout to have any
# effect at all).
#
# This layer used to curl install.sh from `main` and let the installer clone
# `main` unpinned, then force-reinstall a PyPI wheel over site-packages. The
# wheel pin was therefore INERT for the running agent, and the effective agent
# was "whatever upstream main happened to be on the day the image was built" —
# two builds of this identical Dockerfile produced two different agents. The
# image live on 2026-09-03 carries checkout 56526bc0 (upstream 0.20.1,
# 2026-08-16) while the wheel pin claimed 0.19.0.
#
# The installer and the checkout are both taken at HERMES_COMMIT, so the build
# is reproducible.
#
# THE INSTALLER IS VENDORED, NOT DOWNLOADED (2026-09-29)
# ------------------------------------------------------
# vendor/hermes-agent/install.sh is a byte copy of upstream scripts/install.sh
# at HERMES_COMMIT. This step used to be
#   curl -fsSL https://raw.githubusercontent.com/.../${HERMES_COMMIT}/scripts/install.sh | bash -s -- ...
# and on 2026-09-29 every build of the runtime 0.4.92 bump failed on it:
#   - raw.githubusercontent.com answered HTTP 429 to all four builds, on two
#     runner hosts, so no image could be built while that limit lasted.
#   - The pipe hid the failure. The RUN had no pipefail, so the step's status
#     was bash's: bash read an empty script and exited 0, and the build failed
#     one step later with
#     "fatal: cannot change to '/home/agent/.hermes/hermes-agent'".
# Now nothing is downloaded and nothing is piped. The copy must hash to
# HERMES_INSTALL_SH_SHA256 before it runs, so an edited copy fails this step
# by name. The CI gateway-live-roundtrip job compares the copy with
# scripts/install.sh at HERMES_COMMIT upstream, so it cannot drift from the
# pin. Moving HERMES_COMMIT means refreshing the copy and the digest in the
# same change; vendor/hermes-agent/README.md has the commands.
#
# The installer's own network steps are unchanged: it still clones the
# checkout from github.com (with its own retries) and pins it with --commit,
# and the rev-parse assertion below still decides whether the pin held.
ARG HERMES_COMMIT=29112bef099274229cadff79cdff7bf7b99c4b77
ARG HERMES_INSTALL_SH_SHA256=85ef536d455e51ab67aa74d79272efd49fe717597dbaadfd3cca179a905f4706
COPY --chown=agent:agent vendor/hermes-agent/install.sh /tmp/hermes-install.sh
USER agent
WORKDIR /home/agent
RUN set -eu; \
    echo "${HERMES_INSTALL_SH_SHA256}  /tmp/hermes-install.sh" | sha256sum -c -; \
    bash /tmp/hermes-install.sh --skip-setup --commit "${HERMES_COMMIT}" --force-commit; \
    rm -f /tmp/hermes-install.sh
# Never trust an installer to have honoured a flag: the installer treats an
# ignored --commit as a WARNING and still exits 0. Assert the checkout is
# actually at the pin, and fail the build if it is not. This is the whole
# reproducibility guarantee, so it is checked, not assumed.
RUN set -eu; \
    actual="$(git -C /home/agent/.hermes/hermes-agent rev-parse HEAD)"; \
    if [ "$actual" != "${HERMES_COMMIT}" ]; then \
      echo "FATAL: hermes checkout is at $actual, expected ${HERMES_COMMIT}." >&2; \
      echo "       install.sh ignored --commit; the image would ship unpinned upstream main." >&2; \
      exit 1; \
    fi; \
    echo "hermes checkout pinned at $actual"
# hermes installer symlinks ~/.hermes/hermes-agent/venv/bin/hermes into
# ~/.local/bin/hermes, so ~/.local/bin is the only PATH entry we need.
ENV PATH="/home/agent/.local/bin:${PATH}"

# --- Molecule A2A platform plugin (post-demo: native push parity) ---
# Two refs are installed into the same venv that the upstream installer
# created above:
#
#   1. hermes-agent itself is NOT installed here any more. The upstream
#      installer above already did `uv pip install -e .[all]` from the
#      checkout it pinned to HERMES_COMMIT, and the checkout is what the
#      gateway imports (see the sys.path[0] note above). The old
#      `--force-reinstall hermes-agent==${HERMES_VERSION}` line pinned the
#      one copy nothing imports; keeping it would reintroduce exactly the
#      wheel-vs-checkout skew this change removes. FORK RETIRED (2026-07-22):
#      the molecule-ai/hermes-agent fork existed only to carry the
#      `register_platform_adapter` socket from the era before upstream had
#      one. Upstream shipped a superior socket in #17751 (merged 2026-04-30:
#      `ctx.register_platform(...)` + open Platform enum +
#      gateway/platform_registry.py), our PR #18775 was closed as superseded
#      (2026-05-03), and the A2A plugin has been dual-mode since May.
#   2. The Molecule A2A platform plugin, auto-discovered via hermes's
#      `hermes_agent.plugins` entry-point group (registers through
#      ctx.register_platform, #17751).
#
# HERMES_PYPI_FLOOR is the lowest PyPI release that would carry everything
# HERMES_COMMIT carries. PyPI's newest hermes-agent is still 0.19.0
# (2026-07-20) — the fixes we need exist only as git refs — so the pin above
# is an UNRELEASED third-party ref, taken with owner authorisation. The
# upstream-sync workflow compares PyPI's latest against this floor and files
# the "you can go back to a published release now" PR when one appears.
ARG HERMES_PYPI_FLOOR=0.21.0
ARG HERMES_PLATFORM_MOLECULE_A2A_REF=93d43d772470eb3ddc781858dc5629f3464eed99
# The hermes installer uses uv to create the venv and doesn't seed pip
# into it. Bootstrap pip first via ensurepip, then install the plugin.
RUN /home/agent/.hermes/hermes-agent/venv/bin/python3 -m ensurepip --upgrade && \
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
