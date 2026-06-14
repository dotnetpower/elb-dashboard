#!/usr/bin/env bash
# Shared helpers for the repo's version-controlled git hooks.
#
# Responsibility: provide path-classification + pretty logging used by the
# pre-commit and pre-push hooks so each hook only runs the CI checks that are
# relevant to the files actually changing. Keeping this in one place means the
# two hooks (and the installer) stay in sync with what GitHub Actions gates on.
# Edit boundaries: pure bash, no network, no Azure. Anything that needs the
# venv calls `uv run` so it works from a clean clone after `uv sync`.
# Key entry points: `hook_log`, `hook_run`, `hook_run_in`, `paths_touch_api`, `paths_touch_docs`.
# Risky contracts: the path globs here MUST mirror the `paths:` filters in
# .github/workflows/test.yml and .github/workflows/docs.yml. When CI's path
# filters change, update these too.
# Validation: `bash scripts/dev/git-hooks/pre-commit` on a dirty tree.

set -euo pipefail

# Resolve repo root regardless of where the hook is invoked from.
HOOK_REPO_ROOT="$(git rev-parse --show-toplevel)"

# ANSI helpers (no-op when not a TTY).
if [[ -t 2 ]]; then
  _C_BOLD=$'\033[1m'; _C_RED=$'\033[31m'; _C_GREEN=$'\033[32m'
  _C_YELLOW=$'\033[33m'; _C_DIM=$'\033[2m'; _C_RESET=$'\033[0m'
else
  _C_BOLD=""; _C_RED=""; _C_GREEN=""; _C_YELLOW=""; _C_DIM=""; _C_RESET=""
fi

hook_log() { printf '%s[hooks]%s %s\n' "$_C_DIM" "$_C_RESET" "$*" >&2; }
hook_ok() { printf '%s[hooks] ✓ %s%s\n' "$_C_GREEN" "$*" "$_C_RESET" >&2; }
hook_warn() { printf '%s[hooks] ! %s%s\n' "$_C_YELLOW" "$*" "$_C_RESET" >&2; }
hook_err() { printf '%s[hooks] ✗ %s%s\n' "$_C_RED" "$*" "$_C_RESET" >&2; }

# Run a labelled check inside a specific directory; on failure print a clear
# message and propagate the non-zero exit so the commit/push is blocked. The
# directory parameter lets the pre-push hook validate an isolated worktree
# checked out at the pushed commit (CI's clean checkout) instead of the dirty
# working tree.
hook_run_in() {
  local dir="$1" label="$2"; shift 2
  hook_log "running: ${_C_BOLD}${label}${_C_RESET}"
  if ( cd "$dir" && "$@" ); then
    hook_ok "$label"
  else
    local rc=$?
    hook_err "$label FAILED (exit $rc) — this is the same check CI runs, so the push/commit is blocked"
    return "$rc"
  fi
}

# Run a labelled check in the repo root (the working tree). Thin wrapper over
# hook_run_in kept for the pre-commit hook and manual invocations.
hook_run() {
  local label="$1"; shift
  hook_run_in "$HOOK_REPO_ROOT" "$label" "$@"
}

# Does the given newline-separated file list contain anything that the Tests
# workflow gates on? (api/**, pyproject.toml, uv.lock, pytest.ini)
paths_touch_api() {
  grep -qE '^(api/|pyproject\.toml$|uv\.lock$|pytest\.ini$)' <<<"$1"
}

# Does the list contain anything the Publish Docs workflow gates on?
# (mkdocs.yml, docs/**, scripts/docs/**) — web/** also triggers CI but the
# heavy mock-preview build is skipped locally; the failure we actually guard
# against (missing nav entry / bad frontmatter) is caught by the two cheap
# checks below.
paths_touch_docs() {
  grep -qE '^(mkdocs\.yml$|docs/|scripts/docs/)' <<<"$1"
}
