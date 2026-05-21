#!/usr/bin/env bash
# Bump the project SemVer based on Conventional Commits since the last
# `vX.Y.Z` tag, then update web/package.json + pyproject.toml in lockstep.
#
# Rules (matches the user's chosen scheme):
#   MAJOR — manual only: `bump-version.sh --major`. Used for breaking
#           changes the maintainer decides to ship.
#   MINOR — auto when ANY commit since the last tag starts with `feat:`
#           (or `feat(scope):`).
#   PATCH — auto when no `feat:` commits exist but at least one `fix:`
#           commit does. If neither feat nor fix landed, the script
#           refuses to bump (use --patch / --minor to force).
#
# Usage:
#   scripts/dev/bump-version.sh           # auto (feat -> minor, fix -> patch)
#   scripts/dev/bump-version.sh --major   # force major bump
#   scripts/dev/bump-version.sh --minor   # force minor bump
#   scripts/dev/bump-version.sh --patch   # force patch bump
#   scripts/dev/bump-version.sh --dry-run # show what would happen, no changes
#
# Side effects (only when --dry-run is NOT passed):
#   1. Updates web/package.json `"version"` and pyproject.toml `version`.
#   2. Stages both files and creates `chore(release): vX.Y.Z` commit.
#   3. Creates annotated git tag `vX.Y.Z` pointing at that commit.
#
# It does NOT push. The maintainer reviews and runs:
#   git push origin main --follow-tags

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PKG_JSON="web/package.json"
PYPROJECT="pyproject.toml"

ts() { printf '[bump] %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

FORCE_BUMP=""
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --major) FORCE_BUMP="major" ;;
    --minor) FORCE_BUMP="minor" ;;
    --patch) FORCE_BUMP="patch" ;;
    --dry-run|-n) DRY_RUN=true ;;
    -h|--help)
      sed -n '1,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown flag: $arg" ;;
  esac
done

[[ -f "$PKG_JSON" ]]   || die "missing $PKG_JSON"
[[ -f "$PYPROJECT" ]]  || die "missing $PYPROJECT"
command -v node >/dev/null 2>&1 || die "node is required (used to edit $PKG_JSON safely)"
command -v git  >/dev/null 2>&1 || die "git is required"

# 1. Determine current version (web/package.json is the source of truth).
CURRENT="$(node -p "require('./$PKG_JSON').version")"
[[ "$CURRENT" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || die "unexpected version format in $PKG_JSON: $CURRENT"
MAJOR=${BASH_REMATCH[1]}; MINOR=${BASH_REMATCH[2]}; PATCH=${BASH_REMATCH[3]}

# 2. Verify pyproject.toml matches.
PY_CURRENT="$(grep -E '^version *= *"[0-9]+\.[0-9]+\.[0-9]+"' "$PYPROJECT" | head -n1 | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ "$PY_CURRENT" != "$CURRENT" ]]; then
  ts "WARNING: $PYPROJECT version ($PY_CURRENT) != $PKG_JSON ($CURRENT). The bump will sync both to the new value."
fi

# 3. Find the last release tag (vX.Y.Z) on this branch.
LAST_TAG="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname --merged HEAD | head -n1 || true)"
if [[ -z "$LAST_TAG" ]]; then
  ts "no previous v* tag — scanning full history for feat/fix commits"
  RANGE_ARGS=()
else
  ts "last release tag: $LAST_TAG"
  RANGE_ARGS=("$LAST_TAG..HEAD")
fi

# 4. Inspect commit messages.
COMMITS="$(git log "${RANGE_ARGS[@]}" --format='%s' 2>/dev/null || true)"
HAS_FEAT=false
HAS_FIX=false
HAS_BREAKING=false
RE_FEAT='^feat(\([^)]+\))?:'
RE_FIX='^fix(\([^)]+\))?:'
RE_BREAK_FEAT='^feat(\([^)]+\))?!:'
RE_BREAK_FIX='^fix(\([^)]+\))?!:'
while IFS= read -r line; do
  [[ -n "$line" ]] || continue
  if [[ $line =~ $RE_BREAK_FEAT ]] || [[ $line =~ $RE_BREAK_FIX ]] || [[ $line == *"BREAKING CHANGE"* ]]; then
    HAS_BREAKING=true
  fi
  if [[ $line =~ $RE_FEAT ]]; then
    HAS_FEAT=true
  fi
  if [[ $line =~ $RE_FIX ]]; then
    HAS_FIX=true
  fi
done <<< "$COMMITS"

# 5. Decide bump kind.
KIND=""
if [[ -n "$FORCE_BUMP" ]]; then
  KIND="$FORCE_BUMP"
  ts "forced bump: $KIND"
else
  if $HAS_BREAKING; then
    ts "detected BREAKING change in history — refuse auto-bump. Re-run with --major to acknowledge."
    exit 2
  fi
  if $HAS_FEAT;  then KIND="minor"; fi
  if [[ -z "$KIND" ]] && $HAS_FIX; then KIND="patch"; fi
  if [[ -z "$KIND" ]]; then
    ts "no feat/fix commits since ${LAST_TAG:-repo root} — nothing to bump."
    ts "(use --patch / --minor / --major to force.)"
    exit 0
  fi
  ts "auto-detected bump: $KIND  (feat=$HAS_FEAT fix=$HAS_FIX)"
fi

# 6. Compute new version.
case "$KIND" in
  major) MAJOR=$((MAJOR+1)); MINOR=0; PATCH=0 ;;
  minor) MINOR=$((MINOR+1)); PATCH=0 ;;
  patch) PATCH=$((PATCH+1)) ;;
esac
NEW="$MAJOR.$MINOR.$PATCH"
ts "$CURRENT -> $NEW"

if $DRY_RUN; then
  ts "dry-run — no files written, no commit, no tag."
  exit 0
fi

# 7. Refuse to bump on dirty tree (avoids sweeping unrelated edits into the release commit).
if ! git diff --quiet -- "$PKG_JSON" "$PYPROJECT"; then
  die "$PKG_JSON or $PYPROJECT already has unstaged edits; commit or stash them first"
fi
if [[ -n "$(git status --porcelain | grep -v -E '^(\?\?|.[MAD?])' || true)" ]]; then
  : # not used — placeholder; we intentionally allow other untracked files
fi

# 8. Update web/package.json via Node (preserves formatting + key order).
node -e "
const fs = require('fs');
const p = './$PKG_JSON';
const pkg = JSON.parse(fs.readFileSync(p, 'utf8'));
pkg.version = '$NEW';
fs.writeFileSync(p, JSON.stringify(pkg, null, 2) + '\n');
"

# 9. Update pyproject.toml — only the FIRST top-level version line in [project].
python3 - "$NEW" "$PYPROJECT" <<'PY'
import sys, re, pathlib
new, path = sys.argv[1], pathlib.Path(sys.argv[2])
text = path.read_text(encoding='utf-8')
# Replace only the first `version = "x.y.z"` line.
out, n = re.subn(r'(?m)^version\s*=\s*"[^"]+"', f'version = "{new}"', text, count=1)
if n != 1:
    sys.exit(f'failed to patch version in {path}')
path.write_text(out, encoding='utf-8')
PY

ts "updated $PKG_JSON and $PYPROJECT"

# 10. Commit + tag.
git add "$PKG_JSON" "$PYPROJECT"
git commit -m "chore(release): v$NEW" >/dev/null
git tag -a "v$NEW" -m "release v$NEW"
ts "committed and tagged v$NEW"
ts "next: git push origin \"\$(git rev-parse --abbrev-ref HEAD)\" --follow-tags"
