#!/usr/bin/env bash
# Local mkdocs build wrapper.
#
# Mirrors the docs.yml CI step: DISABLE_MKDOCS_2_WARNING=true + --strict by
# default so local builds catch broken links / missing refs the same way CI
# does. Pass extra args to override (e.g. drop --strict for a quick check).
#
# Usage:
#   scripts/docs/build.sh                # uv run mkdocs build --strict
#   scripts/docs/build.sh --clean        # forwarded to mkdocs

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

export DISABLE_MKDOCS_2_WARNING=true

if [[ $# -eq 0 ]]; then
  exec uv run mkdocs build --strict
fi
exec uv run mkdocs build "$@"
