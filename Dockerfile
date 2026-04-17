FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -u 1000 -m -s /bin/bash agent
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python3 -c "import molecule_runtime.preflight as pf; pf.SUPPORTED_RUNTIMES.add('hermes')" && \
    SITE=$(python3 -c 'import molecule_runtime.preflight as p; print(p.__file__)') && \
    sed -i "s/SUPPORTED_RUNTIMES = {/SUPPORTED_RUNTIMES = {'hermes', 'gemini-cli',/" "$SITE"

COPY adapter.py .
COPY __init__.py .
COPY escalation.py .
COPY executor.py .
COPY providers.py .

ENV ADAPTER_MODULE=adapter

ENTRYPOINT ["molecule-runtime"]
