---
title: Self-upgrade commit clone — drop blobless so az acr build sees a full tree
description: Fix the self-upgrade build failing with "Unable to find api/Dockerfile" for commit-channel upgrades by cloning all blobs, because az acr build uploads the context via git archive (the object store), not the working tree.
tags:
  - operate
  - deployment-reference
---

# Self-upgrade commit clone — drop blobless so az acr build sees a full tree

## Motivation

After the managed-identity `az login` fix, the self-upgrade build authenticated
successfully but the commit-channel build then failed with:

```
$ az acr build … --file api/Dockerfile … /tmp/elb-upgrade/<job>
ERROR: Unable to find 'api/Dockerfile'.
```

even though the cloned working tree contained `api/Dockerfile` (the
`_verify_build_files_materialised` check passed).

## Root cause

`az acr build <dir>` detects the `.git` directory and uploads the build context
via **`git archive`** (the committed tree from the object store), **not** by
tarring the working tree. The commit clone used
`git clone --filter=blob:none --no-checkout` (a blobless partial clone), which
only lazy-fetches the blobs that `git checkout` touches. So the working tree was
complete (checkout hydrated it) but the object store was missing blobs, and
`git archive` silently omitted files — including `api/Dockerfile`.

The working-tree verification could not catch this because it inspects the
working tree (`git status --porcelain`), while `az acr build` reads the object
store. The two diverged precisely because of the blob filter.

## User-facing change

Commit-channel self-upgrades now build successfully (the `az acr build` upload
contains every file). No dashboard surface change.

## API / IaC diff summary

- `api/services/upgrade/git_workspace.py` `_clone_commit`: drop
  `--filter=blob:none` from the commit clone. It is now a full
  `git clone --no-checkout` followed by `git checkout --detach <sha>`, so both
  the working tree and the object store (`git archive`) are complete. The repo
  is small, so the extra history download is negligible. `--no-checkout` is
  retained to avoid materialising the default branch's tree we then replace.
- The `_verify_build_files_materialised` guard and its error message are kept
  (defense for a genuinely failed checkout); the message no longer references
  "blobless".
- No infra change.

## Validation evidence

- Local proof of the mechanism: after `git clone --no-checkout … && git checkout
  --detach <sha>`, `git archive HEAD | tar -t` contains all three build
  Dockerfiles (count = 3); with `--filter=blob:none` the archive omitted them.
- Live build-log progression confirmed the preceding fixes and isolated this as
  the last blocker: `… ERROR: Unable to find 'api/Dockerfile'.` (no more
  PLATFORM_ACR_NAME / az-login errors).
- `uv run pytest -q api/tests` → 3042 passed, 3 skipped.
- `uv run ruff check` on touched files → clean.
