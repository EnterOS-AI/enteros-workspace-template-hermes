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
require_line '          docker tag "${SHA_REF}" "${LOCAL_RUNTIME_REF}"'

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
