---
title: pre-push hook validates the pushed commit, not the dirty working tree
description: The pre-push CI mirror now checks each pushed commit out into a throwaway worktree before running ruff/pytest/mkdocs, so a split-commit (test committed ahead of its source) can no longer pass locally and break GitHub Actions.
tags:
  - ci
  - tooling
---

# 2026-06-14 — pre-push validates the pushed commit (split-commit guard)

## Motivation

Two recent GitHub Actions **Tests** runs went red on `main` even though the
author's local tree was green:

* `62db2e27` (outfmt coercion) failed on `ruff` — a `RUF100 Unused noqa
  directive` that was never run locally before pushing.
* `6d8da45` (path-traversal db) failed on `pytest` — the commit carried four
  `test_sync_external_failed_*` tests whose **source** (`external_jobs.py`
  recovery logic) only landed one commit later in `0787e4c`. At the `6d8da45`
  snapshot the tests asserted behaviour that did not yet exist, so CI failed
  (`assert [] == ['fail-tx-1']`). It self-resolved at the next commit.

Both share one root cause: **CI gates were not mirrored against the committed
snapshot before pushing.** The repo already ships a pre-push hook that runs the
exact CI gates, but (a) the hooks were not installed in this clone, and (b) the
hook ran the gates against the *working tree*. For the split-commit case the
working tree still held the uncommitted source, so a working-tree run would have
been a **false green** — the hook's own header claimed to mirror "the commits
being pushed" while actually testing the dirty tree.

## User-facing change

This is a contributor-tooling change; no runtime/API/UI behaviour changes.

* **`scripts/dev/git-hooks/pre-push`** now validates the *committed snapshot*
  being pushed. Each distinct pushed tip SHA is checked out into a throwaway
  `git worktree` (CI's clean checkout) and `ruff` / `pytest` / `mkdocs build
  --strict` run there. Uncommitted working-tree changes can no longer mask a
  broken commit. Worktrees are torn down on every exit (success, failure, or
  interrupt) via an `EXIT` trap, so nothing leaks into `git worktree list`. If a
  worktree cannot be created the hook falls back to validating the working tree
  and warns. Manual invocation (no refs on stdin) keeps validating the working
  tree as before. Path filtering is unchanged — a docs-only push never pays the
  pytest cost and vice-versa.
* **`scripts/dev/git-hooks/_lib.sh`** gains `hook_run_in <dir> <label> <cmd…>`
  so a check can run inside an arbitrary checkout directory; `hook_run` is now a
  thin wrapper over it for the repo root (pre-commit behaviour unchanged).
* The hooks were installed in this clone via `scripts/dev/install-git-hooks.sh`
  (`core.hooksPath=scripts/dev/git-hooks`), which the charter mandates "once per
  clone". The `ruff` failure class is caught by the pre-commit hook; the
  split-commit class is caught by the hardened pre-push hook.

## IaC / API diff summary

None. Only contributor git hooks under `scripts/dev/git-hooks/` change.

## Validation evidence

* `bash -n` clean on `_lib.sh`, `pre-push`, `pre-commit`.
* **Split-commit is now blocked**: feeding the historical broken push
  (`6d8da45` with remote `968b26b`) to the hook on stdin exits **1** with
  `2 failed, 3603 passed` (the four recovery tests), proving the committed
  snapshot — not the working tree — is what gets tested. The old working-tree
  run would have passed.
* **Good commits still pass**: feeding the current good `HEAD` (`01e8ede` with
  remote `0787e4c`) exits **0** after `ruff` + `pytest` (3606 passed) +
  `mkdocs build --strict`.
* **No worktree leak**: `git worktree list` shows only `main` after both runs;
  the `EXIT` trap removed every `/tmp/elb-prepush.*` checkout.
* CI-equivalent clean-HEAD mirror (in a detached worktree at `01e8ede`):
  `ruff` clean, `pytest` 3606 passed / 3 skipped, frontmatter guard OK,
  `mkdocs build --strict` OK — confirming `origin/main` is green.
