# Auto warm re-runs after stop/start on `node_disk` clusters

## Motivation

A user reported that when **Performance → Node OS disk** (`warm_cache_mode=node_disk`)
is active, stopping and starting the AKS cluster left the database cold: auto warm
never re-ran.

Root cause (code-grounded): the dashboard's only mechanism for re-triggering warmup
after an `az aks stop`/`start` cycle is the **node-name staleness heuristic**. Warmup
Jobs are pinned to a `nodeName`; when stop/start rotates the VMSS instance names,
`_mark_stale_warmup_nodes` flips the database to `Stale`, and
`reconcile_auto_warmup_preferences` clears the warm state and re-enqueues.

`node_disk` pins a **Managed OS disk** instead of an ephemeral one. A Managed OS disk
persists across `az aks stop`/`start` and reattaches to the *same* VMSS instances, so the
node names stay stable. The pre-stop warmup Jobs are therefore **not** flagged `Stale`,
the database still reports `Ready`, and the reconciler hits its `Ready` skip — so it never
re-warms even though the node RAM page cache is always cold after a deallocate.

`start_aks` already enqueued `reconcile_auto_warmup(..., force=True)` precisely to override
this, but `force` was a **dead parameter**: declared on
`reconcile_auto_warmup_preferences` and threaded through the Celery task, yet never read in
the body. So the forced post-start reconcile behaved exactly like the periodic one and
skipped the `Ready` database. Ephemeral clusters happened to self-heal (names rotate →
`Stale` → enqueue); `node_disk` clusters never did.

## User-facing change

On a `node_disk` cluster, stopping then starting the cluster now re-runs auto warm for the
configured databases, exactly like an ephemeral cluster. The on-disk database survives, so
the re-warm is fast — only the `vmtouch` (RAM page cache) step re-runs; the download is
skipped.

No UI change. The periodic (un-forced) reconcile still skips a database that is genuinely
warm, so there is no extra warmup churn during normal operation.

## API / IaC diff summary

Backend only (no IaC change):

- `api/services/auto_warmup_reconcile.py`
  - The `Ready`/`Loading` skip is now `if warm_state in {"Ready","Loading"} and not force`.
    A forced pass (only `start_aks` sends `force=True`) re-enqueues instead of skipping.
  - When same-generation warmup Jobs are still present, the task is told to do a full
    release first via `force_rewarm`. The trigger is
    `forced_rewarm = bool(warm_meta) and (force or warm_state == "Failed")`:
    - `force` covers the post stop/start re-warm of a still-`Ready`/`Loading` DB on a
      `node_disk` cluster (stable node names → the pre-stop Jobs are not Stale → ensure
      would otherwise no-op).
    - `warm_state == "Failed"` covers a prior warmup that left Failed Jobs pinned to LIVE
      nodes. On `node_disk` their names are stable, so the node-staleness sweep keeps them
      and ensure would skip recreating them **forever** (the DB stays `Failed` and the beat
      reconcile busy-loops every 120 s without converging). Force-releasing clears them so
      the retry actually re-runs. This fires even on an un-forced beat tick because a
      lingering `Failed` is terminal-bad and must be cleared to recover.
  - The flag is set in the `warmup_database` task kwargs and the seeded `JobState`
    payload. All other guards are unchanged: `not_downloaded`, `update_required`, the
    Ready-node gate, and the in-flight lock still apply, so `force` cannot create duplicate
    warmups.
- `api/tasks/storage/warmup.py`
  - `warmup_database` gains `force_rewarm: bool = False`. When true, it calls
    `k8s_release_warmup_cache(db)` (a full collection delete of the database's warmup Jobs)
    **before** `k8s_ensure_job_manifests`. Without this, ensure sees the existing
    (non-stale) Job names and no-ops, so the RAM cache would stay cold on `node_disk`.
    The release uses the same Background-deletion path already proven by the ephemeral
    stale-release, runs inside the existing planning try-block (so a failure surfaces as a
    Celery retry), and is recorded in the `applying_warmup_jobs` progress checkpoint as
    `force_released_jobs`.
  - **Partial-failure guard**: after recording the release in the progress checkpoint, the
    task raises if `force_release_summary["status"] != "released"` (i.e. a delete returned a
    non-2xx/404 and a stale Job survived). Without this, the surviving Job name would make
    `k8s_ensure_job_manifests` skip recreating that shard, warming only a subset while the
    task reported success. Raising routes it through Celery autoretry so the release re-runs.

## Validation evidence

- `uv run pytest -q api/tests/test_auto_warmup.py` → 60 passed, including:
  - `test_reconcile_auto_warmup_force_reenqueues_ready_db` — forced reconcile re-warms a
    `Ready` database and sets `force_rewarm=True`.
  - `test_reconcile_auto_warmup_skips_ready_db_without_force` — periodic reconcile still
    skips a warm database.
  - `test_warmup_database_force_rewarm_drops_existing_jobs` — `force_rewarm` releases the
    database's Jobs before ensure (ordering asserted).
  - `test_warmup_database_force_rewarm_defaults_off` — no release without `force_rewarm`.
- `uv run pytest -q api/tests/test_tasks_facade_contract.py` → green (no new facade
  string-target monkeypatches introduced).
- `uv run pytest -q api/tests` → 2846 passed, 3 skipped.
- `uv run ruff check api/services/auto_warmup_reconcile.py api/tasks/storage/warmup.py
  api/tests/test_auto_warmup.py` → all checks passed.
