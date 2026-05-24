#!/usr/bin/env bash
# Print the same source version stamp shown in the SPA header.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/version-stamp.sh [--update-readme]

Prints the source stamp in the same shape as the local SPA header:
  v<major>.<minor>.<commits-since-latest-v-tag> · <short-sha>

Options:
  --update-readme  Replace the README Source Stamp badge between markers.
USAGE
}

update_readme=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --update-readme) update_readme=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

read_release_version() {
  if command -v node >/dev/null 2>&1; then
    node -p "require('./web/package.json').version" 2>/dev/null && return 0
  fi
  grep -E '"version"[[:space:]]*:' web/package.json \
    | head -n1 \
    | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
}

release_build_number() {
  local latest_tag=""
  latest_tag="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname --merged HEAD 2>/dev/null | head -n1 || true)"
  if [[ -n "$latest_tag" ]]; then
    git rev-list --count "$latest_tag..HEAD" 2>/dev/null || printf '0\n'
  else
    git rev-list --count HEAD 2>/dev/null || printf '0\n'
  fi
}

format_build_version() {
  local release_version="$1"
  local build_number="$2"
  IFS=. read -r major minor patch extra <<<"$release_version"
  if [[ -n "${major:-}" && -n "${minor:-}" && -n "${patch:-}" && -z "${extra:-}" && "$build_number" =~ ^[0-9]+$ ]]; then
    printf '%s.%s.%s\n' "$major" "$minor" "$build_number"
  else
    printf '%s\n' "$release_version"
  fi
}

release_version="$(read_release_version)"
build_number="$(release_build_number)"
short_sha="$(git rev-parse --short HEAD 2>/dev/null || printf 'dev')"
stamp="v$(format_build_version "$release_version" "$build_number") · $short_sha"

if ! $update_readme; then
  printf '%s\n' "$stamp"
  exit 0
fi

encoded_stamp="$(python3 - "$stamp" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
)"

replacement="<!-- ELB_SOURCE_STAMP_START -->[![Source Stamp](https://img.shields.io/badge/source-${encoded_stamp}-2f6fed)](./scripts/dev/version-stamp.sh)<!-- ELB_SOURCE_STAMP_END -->"

python3 - "$replacement" <<'PY'
import pathlib
import sys

replacement = sys.argv[1]
path = pathlib.Path("README.md")
text = path.read_text(encoding="utf-8")
start = "<!-- ELB_SOURCE_STAMP_START -->"
end = "<!-- ELB_SOURCE_STAMP_END -->"
start_idx = text.find(start)
end_idx = text.find(end)
if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
    raise SystemExit("README.md is missing ELB_SOURCE_STAMP markers")
end_idx += len(end)
path.write_text(text[:start_idx] + replacement + text[end_idx:], encoding="utf-8")
PY

printf '%s\n' "$stamp"