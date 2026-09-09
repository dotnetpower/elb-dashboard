#!/usr/bin/env bash
# Fail when the checked-in OpenAPI TypeScript declaration is missing or stale.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXPECTED="$REPO_ROOT/web/src/api/generated/openapi.d.ts"
GENERATED="$(mktemp "${TMPDIR:-/tmp}/elb-openapi-types.XXXXXX.d.ts")"
trap 'rm -f "$GENERATED"' EXIT

"$REPO_ROOT/scripts/dev/generate-openapi-types.sh" "$GENERATED"
if [[ ! -f "$EXPECTED" ]]; then
  echo "ERROR: generated OpenAPI types are missing: $EXPECTED" >&2
  exit 1
fi
if ! cmp -s "$EXPECTED" "$GENERATED"; then
  echo "ERROR: generated OpenAPI types are stale." >&2
  diff -u "$EXPECTED" "$GENERATED" || true
  echo "Run: npm --prefix web run generate:api-types" >&2
  exit 1
fi
echo "Generated OpenAPI types are current."