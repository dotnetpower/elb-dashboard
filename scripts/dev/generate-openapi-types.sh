#!/usr/bin/env bash
# Generate TypeScript declarations from the current FastAPI OpenAPI schema.
#
# The output lives in a separate generated namespace. Existing hand-written API
# clients keep their imports unchanged; new surfaces may opt into these types.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA_FILE="$(mktemp "${TMPDIR:-/tmp}/elb-openapi.XXXXXX.json")"
OUTPUT_FILE="${1:-$REPO_ROOT/web/src/api/generated/openapi.d.ts}"
trap 'rm -f "$SCHEMA_FILE"' EXIT

cd "$REPO_ROOT"
uv run python scripts/dev/check_openapi_contract.py --schema-output "$SCHEMA_FILE"
mkdir -p "$(dirname "$OUTPUT_FILE")"
pushd web >/dev/null
npx openapi-typescript "$SCHEMA_FILE" -o "$OUTPUT_FILE"
popd >/dev/null