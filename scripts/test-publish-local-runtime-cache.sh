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

require_order() {
  local block="$1" label="$2" expected line previous=0
  shift 2
  for expected in "$@"; do
    line="$(grep -nF -- "$expected" <<<"$block" | head -1 | cut -d: -f1 || true)"
    if [ -z "$line" ] || [ "$line" -le "$previous" ]; then
      printf 'FAIL: %s ordering is invalid at: %s\n' "$label" "$expected" >&2
      exit 1
    fi
    previous="$line"
  done
}

# The publish job stages a digest-verified, unique local candidate without
# moving the live runtime tag before control-plane promotion succeeds.
require_line '          LOCAL_CANDIDATE_REF="workspace-template-hermes:pending-${GITHUB_SHA}"'
require_line '            docker tag "${SHA_REF}" "${LOCAL_CANDIDATE_REF}"'
require_line '            CANDIDATE_IMAGE_META="$(docker image inspect --format '\''{{.Id}}|{{json .RepoDigests}}'\'' "${LOCAL_CANDIDATE_REF}")"'
require_line '            if ! printf '\''%s\n'\'' "${CANDIDATE_IMAGE_META}" | grep -Fq "@${DIGEST}"; then'

publish_block="$(sed -n '/- name: Push image to registry/,/- name: Release runner-local candidate image/p' "$WORKFLOW")"
require_order "$publish_block" "publish" \
  'if [ "${GITHUB_REF}" = "refs/heads/main" ]; then' \
  'docker tag "${SHA_REF}" "${LOCAL_CANDIDATE_REF}"' \
  'CANDIDATE_IMAGE_META="$(docker image inspect' \
  'if ! printf '\''%s\n'\'' "${CANDIDATE_IMAGE_META}" | grep -Fq "@${DIGEST}"; then' \
  'echo "digest=${DIGEST}" >> "$GITHUB_OUTPUT"'
if grep -Fq 'docker tag "${SHA_REF}" "${LOCAL_RUNTIME_REF}"' <<<"$publish_block"; then
  echo 'FAIL: publish job moves the live local tag before pin promotion' >&2
  exit 1
fi

# The live tag moves only after promote + read-back verification. A successful
# handoff removes its pending alias; ambiguous outcomes preserve it.
require_line '  retain-local-runtime:'
require_line '    needs: [resolve-version, publish, promote-pin, verify-pin]'
require_line '    if: ${{ always() && github.ref == '\''refs/heads/main'\'' }}'
require_line '            echo "::warning::preserving ${LOCAL_CANDIDATE_REF}; pin outcome is ambiguous"'
require_line '          docker tag "${LOCAL_CANDIDATE_REF}" "${LOCAL_RUNTIME_REF}"'
require_line '          LOCAL_IMAGE_META="$(docker image inspect --format '\''{{.Id}}|{{json .RepoDigests}}'\'' "${LOCAL_RUNTIME_REF}")"'
require_line '          echo "::notice::retained ${LOCAL_RUNTIME_REF} locally with verified digest ${IMAGE_DIGEST}"'
require_line '          docker image rm -f "${LOCAL_CANDIDATE_REF}" >/dev/null 2>&1 || true'

finalize_block="$(sed -n '/^  retain-local-runtime:/,$p' "$WORKFLOW")"
require_order "$finalize_block" "finalize" \
  'if [ "${PUBLISH_RESULT}" != "success" ] || [ "${PROMOTE_RESULT}" != "success" ] || [ "${VERIFY_RESULT}" != "success" ]; then' \
  'CANDIDATE_IMAGE_META="$(docker image inspect' \
  'docker tag "${LOCAL_CANDIDATE_REF}" "${LOCAL_RUNTIME_REF}"' \
  'LOCAL_IMAGE_META="$(docker image inspect' \
  'echo "::notice::retained ${LOCAL_RUNTIME_REF} locally with verified digest ${IMAGE_DIGEST}"' \
  'docker image rm -f "${LOCAL_CANDIDATE_REF}"'

reclaim_block="$(sed -n '/- name: Reclaim stale runner-local Hermes candidates/,/- name: Set up Docker Buildx/p' "$WORKFLOW")"
require_order "$reclaim_block" "pending preflight" \
  'docker image ls' \
  '--filter "reference=workspace-template-hermes:pending-*"' \
  'live_id="$(docker image inspect --format' \
  'if [ -n "${live_id}" ] && [ "${pending_id}" = "${live_id}" ]; then' \
  'docker image rm "${ref}"' \
  'unresolved_pending_refs+=("${ref}")' \
  'if [ "${#unresolved_pending_refs[@]}" -gt 0 ]; then' \
  'exit 1'
if grep -Fq 'docker image rm -f "${ref}"' <<<"$reclaim_block"; then
  echo 'FAIL: unresolved pending candidates must never be force-removed' >&2
  exit 1
fi

if grep -Fq 'docker image prune' "$WORKFLOW"; then
  echo 'FAIL: image prune can race digest verification and docker run on the shared daemon' >&2
  exit 1
fi

echo 'publish local-runtime cache contract: PASS'
