---
title: CI-mirror git hooks block red builds before push
description: Version-controlled pre-commit and pre-push git hooks reproduce the Tests and Publish Docs GitHub Actions gates locally, scoped to the files each commit or push actually touches, so a broken commit never reaches the remote.
tags:
  - infra
  - contributor
---

# CI-mirror git hooks block red builds before push

## Motivation

Two GitHub Actions workflows gate `main` — **Tests**
([.github/workflows/test.yml](../../../.github/workflows/test.yml): `ruff check api`
+ `pytest -q api/tests`) and **Publish Docs**
([.github/workflows/docs.yml](../../../.github/workflows/docs.yml): frontmatter
canon guard + `mkdocs build --strict`). Recent pushes turned the dashboard red
twice for avoidable reasons: a doc page added without a `nav:` entry (fails
`--strict`) and a flaky subprocess test. Nothing reproduced the exact CI checks
locally before a push, so the failures were only visible after the fact.

## User-facing change

A version-controlled git-hook set now mirrors both gates on the developer's
machine, opt-in via a single installer:

```bash
scripts/dev/install-git-hooks.sh   # sets core.hooksPath=scripts/dev/git-hooks
```

* **pre-commit** (fast, staged files only): runs `ruff check api` when `api/**`
  is staged and the docs frontmatter guard when `docs/**` / `mkdocs.yml` is
  staged. ~2 s.
* **pre-push** (full CI mirror): runs `pytest -q api/tests` and/or
  `mkdocs build --strict`, each only when the pushed commit range actually
  touches the paths that the corresponding workflow gates on (a docs-only push
  skips pytest and vice-versa). Branch deletions are a true no-op.

Bypass for genuine emergencies with `git commit/push --no-verify` or
`ELB_SKIP_HOOKS=1`. Uninstall with `git config --unset core.hooksPath`.

The agent charter ([.github/copilot-instructions.md](../../../.github/copilot-instructions.md))
gains a **"CI parity"** subsection under *Validation before marking done* that
documents the workflow→local-command mapping and mandates installing the hooks.

## File diff summary

| File | Change |
| --- | --- |
| `scripts/dev/git-hooks/_lib.sh` | New — path classification (`paths_touch_api` / `paths_touch_docs`, mirroring the workflows' `paths:` filters) + labelled `hook_run` logging. |
| `scripts/dev/git-hooks/pre-commit` | New — staged-file ruff + frontmatter guard. |
| `scripts/dev/git-hooks/pre-push` | New — pushed-range pytest + `mkdocs build --strict`, scoped per touched paths; deletion-only push is a no-op. |
| `scripts/dev/install-git-hooks.sh` | New — idempotent installer that sets `core.hooksPath`. |
| `.github/copilot-instructions.md` | Added the "CI parity" subsection. |
| `scripts/dev/README.md` | Documented the installer and `git-hooks/` directory. |

No API or IaC surface changes.

## Validation evidence

* `bash scripts/dev/install-git-hooks.sh` → `core.hooksPath=scripts/dev/git-hooks`.
* pre-commit on a staged `mkdocs.yml`: ran the frontmatter guard, `✓`, exit 0.
* pre-push deletion-only stdin (`local_sha=0000…`): true no-op, exit 0.
* pre-push on a real range: correctly ran `ruff` + `pytest` and **blocked** on a
  failing test (`assert 124 == 0`), proving the gate works end-to-end.
* Path classification spot-check: `api/main.py` → api=Y docs=n; `docs/foo.md` →
  api=n docs=Y; `mkdocs.yml` → docs=Y; `web/src/App.tsx` → both n.
* `bash -n` clean on all three hook scripts.
