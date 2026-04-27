FROM python:3.11-slim

# System deps:
#   curl         — hermes installer + loopback health probe in start.sh
#   ca-certificates — TLS for all the outbound installs
#   git          — hermes installer clones the repo; also used by agent tools
#   gosu         — drop privileges in start.sh (single-process friendly)
#   xz-utils     — hermes installer extracts a Node 22 tarball (.tar.xz)
#   build-essential — some python deps in hermes `.[all]` extra compile from src
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git gosu xz-utils build-essential \
    && rm -rf /var/lib/apt/lists/*

# Non-root agent user. hermes-agent writes its state into ~/.hermes so
# mounting /home/agent as a persistent volume keeps skills + memory
# across workspace restarts.
RUN useradd -u 1000 -m -s /bin/bash agent

# --- Install molecule_runtime (bridge + A2A server) ---
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY adapter.py .
COPY __init__.py .
COPY executor.py .
COPY scripts/ /app/scripts/
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

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

USER root
WORKDIR /app

ENV ADAPTER_MODULE=adapter \
    HERMES_API_BASE=http://127.0.0.1:8642/v1 \
    API_SERVER_ENABLED=true \
    API_SERVER_HOST=127.0.0.1 \
    API_SERVER_PORT=8642

# start.sh boots `hermes gateway` in the background, waits for :8642
# readiness, then exec's molecule-runtime on :8000.
ENTRYPOINT ["/usr/local/bin/start.sh"]
