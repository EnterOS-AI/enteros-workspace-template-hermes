FROM python:3.11-slim

# System deps: curl for the hermes installer, git for the agent's file/repo
# tools, gosu so start.sh can drop privileges, ca-certificates for TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root agent user. hermes-agent writes its state into ~/.hermes so
# mounting /home/agent as a persistent volume keeps skills + memory
# across workspace restarts.
RUN useradd -u 1000 -m -s /bin/bash agent

# --- Install molecule_runtime (bridge + A2A server) ---
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python3 -c "import molecule_runtime.preflight as pf; pf.SUPPORTED_RUNTIMES.add('hermes')" && \
    SITE=$(python3 -c 'import molecule_runtime.preflight as p; print(p.__file__)') && \
    sed -i "s/SUPPORTED_RUNTIMES = {/SUPPORTED_RUNTIMES = {'hermes', 'gemini-cli',/" "$SITE"

COPY adapter.py .
COPY __init__.py .
COPY executor.py .
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# --- Install the real Nous Research hermes-agent as the agent user ---
# The installer lives under the agent's home (~/.hermes, PATH update in
# .bashrc). Running as root would place it in /root and break discovery.
USER agent
WORKDIR /home/agent
RUN curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
# Make `hermes` available in non-interactive shells (start.sh).
ENV PATH="/home/agent/.local/bin:/home/agent/.hermes/bin:${PATH}"

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
