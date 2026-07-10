#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKFLOW="$ROOT/.gitea/workflows/publish-image.yml"

require_line() {
  local expected="$1"
  if ! grep -Fqx "$expected" "$WORKFLOW"; then
    printf 'FAIL: publish workflow is missing: %s\n' "$expected" >&2
    exit 1
  fi
}

# The co-located control plane resolves this exact local fallback when a pinned
# registry image is cold or unavailable. Retaining the just-verified image under
# this tag prevents a multi-gigabyte pull from exceeding core's 120s CP timeout.
require_line '          LOCAL_RUNTIME_REF="workspace-template-hermes:latest"'
require_line '          if [ "${GITHUB_REF}" = "refs/heads/main" ]; then'
require_line '            docker tag "${SHA_REF}" "${LOCAL_RUNTIME_REF}"'
require_line '            LOCAL_IMAGE_META="$(docker image inspect --format '\''{{.Id}}|{{json .RepoDigests}}'\'' "${LOCAL_RUNTIME_REF}")"'
require_line '            if ! printf '\''%s\n'\'' "${LOCAL_IMAGE_META}" | grep -Fq "@${DIGEST}"; then'
require_line '          docker image prune -f \'
require_line '            --filter "label=org.opencontainers.image.source=https://git.moleculesai.app/${{ github.repository }}" >/dev/null || true'

push_block="$(sed -n '/- name: Push image to registry/,/- name: Release runner-local candidate image/p' "$WORKFLOW")"
previous=0
for expected in \
  'if [ "${GITHUB_REF}" = "refs/heads/main" ]; then' \
  'docker tag "${SHA_REF}" "${LOCAL_RUNTIME_REF}"' \
  'LOCAL_IMAGE_META="$(docker image inspect' \
  'if ! printf '\''%s\n'\'' "${LOCAL_IMAGE_META}" | grep -Fq "@${DIGEST}"; then' \
  'echo "digest=${DIGEST}" >> "$GITHUB_OUTPUT"'; do
  line="$(grep -nF "$expected" <<<"$push_block" | head -1 | cut -d: -f1)"
  if [ -z "$line" ] || [ "$line" -le "$previous" ]; then
    printf 'FAIL: publish workflow ordering is invalid at: %s\n' "$expected" >&2
    exit 1
  fi
  previous="$line"
done

# The generic post-job cleanup must remain scoped to the temporary registry
# aliases. Deleting LOCAL_RUNTIME_REF here reintroduces the cold-pull failure.
cleanup_block="$({
  sed -n '/- name: Release runner-local candidate image/,/^  promote-pin:/p' "$WORKFLOW"
} || true)"
if grep -Fq 'LOCAL_RUNTIME_REF' <<<"$cleanup_block"; then
  echo 'FAIL: post-job cleanup deletes the retained local runtime image' >&2
  exit 1
fi

echo 'publish local-runtime cache contract: PASS'
