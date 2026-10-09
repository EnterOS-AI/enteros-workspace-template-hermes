# Vendored hermes-agent installer

`install.sh` is an unmodified copy of `scripts/install.sh` from
<https://github.com/NousResearch/hermes-agent> at the commit the Dockerfile
pins in `ARG HERMES_COMMIT`. The image build runs this copy instead of
downloading it from raw.githubusercontent.com. The comment above
`ARG HERMES_COMMIT` in the Dockerfile explains why.

upstream_commit: f97608f178d1ffeca59860195ab7da295f7c8e5f
upstream_path: scripts/install.sh
git_blob: 4e68442551a510573d9997ed678091e83b225773

Do not edit `install.sh`. Three checks hold it to upstream:

- Image build: the Dockerfile runs `sha256sum -c` against
  `ARG HERMES_INSTALL_SH_SHA256` before it runs the script.
- `tests/test_vendored_hermes_installer.py`: `upstream_commit` above equals
  `ARG HERMES_COMMIT`, the copy's git blob id equals `git_blob` above, and its
  sha256 equals `ARG HERMES_INSTALL_SH_SHA256`.
- CI job `gateway-live-roundtrip`: the blob id of `scripts/install.sh` at
  `HERMES_COMMIT` in its upstream clone equals the copy's blob id.

## Refreshing after `HERMES_COMMIT` moves

```sh
COMMIT=<new 40-character upstream commit>
curl -fsSL -o vendor/hermes-agent/install.sh \
  "https://raw.githubusercontent.com/NousResearch/hermes-agent/${COMMIT}/scripts/install.sh"
git hash-object vendor/hermes-agent/install.sh    # new git_blob
sha256sum vendor/hermes-agent/install.sh          # new ARG HERMES_INSTALL_SH_SHA256
```

Then, in the same change, set `upstream_commit` and `git_blob` above, and
`ARG HERMES_COMMIT` and `ARG HERMES_INSTALL_SH_SHA256` in the Dockerfile.
To check the blob id against upstream before pushing:

```sh
git clone -q --filter=blob:none --no-checkout \
  https://github.com/NousResearch/hermes-agent.git /tmp/hermes-src
git -C /tmp/hermes-src rev-parse "${COMMIT}:scripts/install.sh"   # must equal git_blob
```
