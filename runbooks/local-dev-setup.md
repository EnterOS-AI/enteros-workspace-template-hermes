# Local development — Hermes workspace template

These commands follow the checks that the repository currently runs in CI.
Local validation does not require a live workspace or production credential.

## Prerequisites

- Python 3.11+
- Git
- Bash
- Access to `git.moleculesai.app` and its package registry
- Docker only when reproducing the image/T4 jobs

## Clone and install test dependencies

```bash
git clone https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-hermes.git
cd molecule-ai-workspace-template-hermes
git switch -c fix/describe-the-change

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip packaging pyyaml jsonschema

rm -rf .molecule-ci-canonical
git clone --depth 1 https://git.moleculesai.app/molecule-ai/molecule-ci.git .molecule-ci-canonical
python3 .molecule-ci-canonical/scripts/install_workspace_dependencies.py --allow-missing
python3 -m pip install -r requirements-dev.txt
```

The canonical installer acquires the private runtime from the Gitea package
registry. Do not route all packages through a private extra index or install a
similarly named public package.

## Static and shell checks

```bash
PROVIDERS_MANIFEST_FILE=internal/providers/providers.yaml \
  python3 .molecule-ci-canonical/scripts/validate-workspace-template.py --static-only
bash scripts/test-derive-provider.sh
bash scripts/test-derive-platform-llm.sh
bash scripts/test-install-prefix-strip.sh
bash scripts/test-load-workspace-config.sh
bash scripts/test-default-model-selection.sh
bash scripts/test-mcp-configs-dir.sh
bash scripts/test-process-liveness.sh
bash scripts/test-publish-local-runtime-cache.sh
```

## Adapter and release contracts

CI intentionally runs the following focused set while a separate executor test
remains outside the required gate:

```bash
python3 -m pytest \
  tests/test_conformance.py \
  tests/test_release_contracts.py \
  tests/test_prepare_runtime_requirements.py \
  tests/test_plugin_installer_cutover.py \
  -v
```

Do not replace this with a claim that every file under `tests/` is a required
gate unless the workflow is changed at the same time.

## Image validation

With Docker and package access available, the basic build is:

```bash
docker build -t workspace-template-hermes:dev .
```

The container is not a standalone `python adapter.py` program. The supported
entrypoint is `start.sh`, which expects platform-provided mounts and runtime
configuration. CI owns the platform-shaped smoke and privilege-conformance
checks; do not invent a local control-plane hostname or fake workspace token.

## Before opening a pull request

```bash
git diff --check
python3 -m pytest --rootdir=tests --import-mode=importlib tests/test_release_contracts.py -q
```

Never commit `.env` files, provider keys, platform tokens, or generated
credential files.
