#!/usr/bin/env bash
# Install the repo's version-controlled git hooks by pointing core.hooksPath at
# scripts/dev/git-hooks. Idempotent — safe to re-run after a fresh clone.
#
# Why core.hooksPath instead of copying into .git/hooks: the hooks live in the
# repo (reviewed, versioned, kept in sync with the CI workflows), and a single
# `git config` line opts in. Undo with `git config --unset core.hooksPath`.
#
# Usage: scripts/dev/install-git-hooks.sh
# Validation: re-run and confirm it reports "already installed".

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="scripts/dev/git-hooks"

cd "$REPO_ROOT"

chmod +x "$HOOKS_DIR"/pre-commit "$HOOKS_DIR"/pre-push 2>/dev/null || true

current="$(git config --local --get core.hooksPath || true)"
if [[ "$current" == "$HOOKS_DIR" ]]; then
  echo "git hooks already installed (core.hooksPath=$HOOKS_DIR)"
else
  git config --local core.hooksPath "$HOOKS_DIR"
  echo "Installed git hooks: core.hooksPath=$HOOKS_DIR"
fi

cat <<'EOF'

Active hooks:
  pre-commit  ruff check api + docs frontmatter guard (fast, staged files only)
  pre-push    pytest api/tests + mkdocs build --strict (full CI mirror)

Bypass once:   git commit --no-verify   /   git push --no-verify
Bypass always: ELB_SKIP_HOOKS=1 git ...
Uninstall:     git config --unset core.hooksPath
EOF
