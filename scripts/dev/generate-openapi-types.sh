#!/usr/bin/env bash
# Generate TypeScript declarations from the current FastAPI OpenAPI schema.
#
# The output lives in a separate generated namespace. Existing hand-written API
# clients keep their imports unchanged; new surfaces may opt into these types.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA_FILE="$(mktemp "${TMPDIR:-/tmp}/elb-openapi.XXXXXX.json")"
OUTPUT_FILE="${1:-$REPO_ROOT/web/src/api/generated/openapi.d.ts}"
GENERATOR="$REPO_ROOT/web/node_modules/.bin/openapi-typescript"
trap 'rm -f "$SCHEMA_FILE"' EXIT

cd "$REPO_ROOT"
uv run python scripts/dev/check_openapi_contract.py --schema-output "$SCHEMA_FILE"
mkdir -p "$(dirname "$OUTPUT_FILE")"
if [[ ! -x "$GENERATOR" ]]; then
	echo "ERROR: local OpenAPI generator is missing; run npm ci --prefix web." >&2
	exit 1
fi
pushd web >/dev/null
"$GENERATOR" "$SCHEMA_FILE" -o "$OUTPUT_FILE"
popd >/dev/null