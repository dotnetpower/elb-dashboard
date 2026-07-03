---
title: DB volume/shard consistency self-heal (prune ghosts + regen shard layout)
description: prepare-db now prunes ghost volumes and regenerates the shard layout for the true volume set, with a manual repair route and an opt-in beat reconciler, preventing the 3-way generation mismatch that failed BLAST with "vol does not match lmdb vol".
tags:
  - blast
  - operate
---

# DB volume/shard consistency self-heal

## Motivation

A production BLAST DB (`core_nt`) drifted into a **3-way generation mismatch** in
Azure Storage and every job on it failed with the coarse "one or more BLAST jobs
failed". Root cause, confirmed against NCBI:

```
NCBI latest (authority) = 79 volumes (00..78)
db metadata njs/ndb     = 79 volumes   (correct)
volume files in Storage = 94 volumes   (79..93 are GHOSTS from a larger snapshot)
shard layout (Kshards/) = 87-vol refs  (shard 09 = 81..86, all ghosts)
```

`core_nt` had shrunk on NCBI, but **prepare-db only copies the new generation's
files — it never prunes the ghost volumes left behind, nor regenerates the shard
alias layout for the new volume count** (`upload_shard_set` is skip-if-exists and
never deletes). A shard alias pointing at volumes outside the LMDB manifest makes
`blastdbcmd -db <shard> -info` fail with "Input db vol does not match lmdb vol",
cascading into every BLAST job on that DB. There was **no cleanup path and no
consistency reconciler**.

## User-facing change

Three tiers of defence, all built on one reusable heal
(`api/services/db/consistency.py`), with the BLAST v5 njs `number-of-volumes`
field as the **authority** (any Storage volume with index >= that count is a
ghost):

1. **Prevention (always on)** — both prepare-db paths (server-side + AKS fanout)
   now run `reconcile_db_consistency(force_reshard=True)` right after a download:
   prune ghost volumes, delete the stale shard layout, and regenerate it for the
   true volume set. A DB can no longer be left inconsistent after an update.
2. **Manual repair** — `POST /api/blast/databases/{db}/shard` now runs the full
   reconcile (prune + regen), so the existing "shard" action is also the
   one-click repair for a drifted DB. A healthy DB has no ghosts, so this is a
   no-op prune + a normal shard rebuild — identical to the old behaviour.
3. **Self-heal (opt-in)** — a beat reconciler
   (`api.tasks.storage.reconcile_db_consistency`, every
   `CELERY_BEAT_DB_CONSISTENCY_SECONDS`, default 1800s) reconciles every prepared
   DB. It is **default-OFF** (`DB_CONSISTENCY_RECONCILE_ENABLED`, charter §12a
   Rule 4) because it deletes Storage blobs; enabling automatic self-heal is an
   explicit operator opt-in.

### Safety guards (why it can't delete the wrong thing)

* **No authority → no prune.** If the njs is missing / unparseable / reports a
  non-positive count, nothing is deleted.
* **50% cap.** If ghosts would exceed half of all volumes (a likely NCBI
  `latest-dir` glitch under-reporting the count), the reconcile ABORTS and logs
  a warning instead of deleting.
* **Non-blocking per-DB lock.** The reconciler holds `prepare_db_lock` non-blocking
  so it can never race a live prepare-db download.
* **Self-correcting.** If a prune succeeds but the follow-up reshard fails, the
  next pass detects the stale layout (`shard_layout_needs_rebuild`) and rebuilds
  it even though no ghosts remain.

## API / IaC diff summary

* New `api/services/db/consistency.py` — `read_authoritative_volume_count`,
  `find_ghost_volumes`, `prune_ghost_volumes`, `delete_shard_layouts`,
  `shard_layout_needs_rebuild`, `reconcile_db_consistency`,
  `reconcile_all_db_consistency`.
* New `api/tasks/storage/reconcile_db_consistency.py` — gated default-OFF beat
  task; registered in the storage facade `__init__.py` + `celery_app.py`
  `beat_schedule`.
* `api/routes/storage/prepare_db.py` + `api/tasks/storage/prepare_db_via_aks.py`
  — auto-shard block replaced with the reconcile.
* `api/routes/blast/databases_shard.py` — shard route runs the full reconcile.
* No Bicep/IaC change (new env vars are optional, default-OFF / default-value).

## Validation

* `uv run pytest -q api/tests/test_db_consistency.py` — 15 new tests: authority
  read, ghost detection, prune guards (no-authority skip, 50% abort, ghost-only
  deletion), reconcile status machine, reconcile-all iteration.
* `uv run pytest -q api/tests` — full suite **4746 passed, 3 skipped** (no
  regression; AKS task test fixture updated for the new reconcile flow).
* `uv run ruff check` — clean.
* Live: the same heal (run manually this session) recovered the drifted customer
  `core_nt` — pruned 15 ghost volumes (79..93), regenerated shard 09 as
  volumes 72..78, warmup 10/10, blastdbcmd passing.
