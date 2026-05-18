# Auto-warmup: delete stale Jobs pinned to removed VMSS nodes

## Motivation
After fix `2026-05-18-autowarmup-reenqueue-and-openapi-update-gate`, the beat
reconciler correctly re-enqueues a `warmup_database` task for any DB that is
not in the `Ready` state. But the task itself still failed for `core_nt`
on the production `ca-elb-control` Container App — every cycle reported
`status="failed", nodes_failed=10`, and `core_nt` never went back to warm.

Investigation showed the root cause is `Job.spec.template.spec.nodeName`
immutability combined with AKS stop/start rotating VMSS instance names:

- ElasticBLAST node-local warmup builds Jobs named `warm-<db>-<shard>` and
  pins each one to a specific VMSS node via `spec.template.spec.nodeName`
  (see `api/services/warmup_jobs.py:database_status_from_warmup_jobs` and
  the Job builder around line 521).
- When AKS is stopped and started, the underlying VMSS instances are
  replaced. The previously-succeeded `warm-core-nt-{00..09}` Jobs still
  exist with `status.succeeded=1, failed=0`, but their `nodeName` points
  at instances that are no longer in the cluster.
- `api/services/k8s_monitoring.py::_mark_stale_warmup_nodes` correctly
  classifies the database as `Stale` in this state (it sets
  `nodes_failed = total_jobs`), which is why the dashboard shows the DB as
  not-warm.
- `api/services/k8s_monitoring.py::_ensure_job_manifests` then refuses to
  recreate the Jobs because the names already exist — it short-circuits
  with `existing.append(name); continue`.
- The result is a permanent failure loop: reconcile fires →
  `warmup_database` runs → ensure finds existing Jobs and does nothing →
  status remains `Stale` → reconcile fires again.

Memory file `/memories/repo/aks-warmup-storage.md` already noted this
hazard: *"After AKS stop/start, completed `elb-db-warmup` Jobs may point
at removed node names; treat them as stale even if `status.succeeded=1`."*

## User-facing change
- After this fix, when the dashboard's auto-warmup reconciler triggers a
  warmup for a database whose Jobs are pinned to nodes no longer in the
  cluster, the worker deletes the stale Jobs (with
  `propagationPolicy=Background` so the pods clean up too) and recreates
  fresh Jobs on the current ready nodes.
- `core_nt` (and any other DB previously stuck after an AKS stop/start)
  returns to `Ready` on its next warmup cycle without manual intervention.
- No UI change beyond what was already visible: the warmup card status
  flips from `Stale` to `Warming` → `Ready` as expected.

## API / IaC diff
- `api/services/k8s_monitoring.py` — new helper
  `k8s_release_stale_warmup_jobs(credential, subscription_id,
  resource_group, cluster_name, db_name, current_node_names,
  namespace='default')`. Lists Jobs labelled
  `app=db-warmup,db=<sanitized>`, compares each Job's
  `spec.template.spec.nodeName` against the current ready-node set, and
  deletes those whose nodeName is no longer in the cluster. Returns
  `{status, database, namespace, deleted: [...], kept: [...], errors:
  [...]}`. Mirrors the existing `k8s_release_warmup_cache` pattern but
  filters per-Job rather than wiping the whole label.
- `api/tasks/storage.py::warmup_database` — calls the new helper between
  `k8s_ensure_warmup_scripts_configmap` and `k8s_ensure_job_manifests`,
  passing the full set of currently-Ready warmup nodes (not the
  per-round `plan.nodes`, so that Jobs still pinned to live but
  not-selected nodes are preserved). The `stale_jobs` summary is added
  to both `_record_task_progress` and the persisted state so the audit
  log shows which Jobs were dropped.

No IaC, infra, or frontend changes.

## Validation evidence
- Targeted tests:
  `uv run pytest -q api/tests/test_k8s_release_stale_warmup_jobs.py`
  → 4 passed (deletes only Jobs on dead nodes; keeps live; skips
  unpinned; reports partial on delete error).
- Existing warmup tests:
  `uv run pytest -q api/tests/test_auto_warmup.py
  api/tests/test_blast_tasks.py api/tests/test_warmup_jobs.py`
  → 92 passed.
- Full suite: `uv run pytest -q api/tests` → **649 passed**.
- Lint: `uv run ruff check api` → All checks passed.
