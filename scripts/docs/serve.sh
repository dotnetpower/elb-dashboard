#!/usr/bin/env bash
# Local mkdocs serve wrapper.
#
# Sets DISABLE_MKDOCS_2_WARNING=true to silence the third-party "ProperDocs"
# advocacy banner emitted by one of the mkdocs plugins. Matches the env var
# already set by .github/workflows/docs.yml so local + CI behave the same.
#
# Usage:
#   scripts/docs/serve.sh                # → http://127.0.0.1:8012/elb-dashboard/
#   scripts/docs/serve.sh -a 127.0.0.1:8013
#   scripts/docs/serve.sh --strict       # any flag is forwarded to mkdocs

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

export DISABLE_MKDOCS_2_WARNING=true
exec uv run mkdocs serve "$@"
