# Fix: Cancelled BLAST job still shows "Running" on the AKS cluster card

## Motivation

A user reported that clicking **Cancel** on a running BLAST job on the
Cluster Plane → AKS card left the job stuck on **RUNNING** instead of
flipping to a cancelled/failed indicator. The Kubernetes Job was never
removed either, so the AKS workload kept burning compute after the
operator thought they had stopped it.

Root cause was two layered bugs that combined into the visible symptom:

### 1. `list_children` exceeded Azure Tables' max page size

`api/tasks/blast/cancel_task.py` walks the parent's split children before
issuing the `kubectl delete jobs` calls:

```python
child_cap = 10_000
children = list(repo.list_children(job_id, limit=child_cap))
```

`JobStateRepository.list_children` passed that `limit` straight through to
`TableClient.query_entities(..., results_per_page=limit)`. **Azure Table
Storage hard-caps `$top` at 1000 entities per response** — asking for more
returns HTTP 400 `InvalidInput / "One of the request inputs is not
valid"` with no rows, which the SDK raises as `HttpResponseError`.

Live worker log on `ca-elb-dashboard` (UTC 2026-05-26 06:20-06:21,
container `worker`):

```
Task api.tasks.blast.cancel[…] retry: Retry in 15s:
  HttpResponseError('One of the request inputs is not valid.
   …ErrorCode:InvalidInput')
…
Task api.tasks.blast.cancel[…] succeeded in 0.30s:
  {'job_id': '98cf480b-…', 'status': 'failed',
   'phase': 'cancel_unavailable',
   'error': 'One of the request inputs is not valid.'}
```

Confirmed terminal state of the row (`stelbdashboard3abp67bppe.jobstate`):

```json
{ "phase": "cancel_unavailable", "status": "failed",
  "error_code": "cancel_unavailable" }
```

Consequence: the cancel task never reached `k8s_cancel_blast_job`. The
Kubernetes Job stayed `Running` and the row hovered at
`status=running` during the ~45 s of Celery retries.

### 2. UI classifier kept reading `status="running"` during cancel

`web/src/components/cards/ClusterBento/jobMapping.ts` had no entries for
`cancelling`, `cancel_unavailable`, `cancel_blocked`,
`cancel_retryable_failure`. Because the cancel task intentionally writes
`status="running"` while it retries (so the reconciler doesn't double-
cancel) and the classifier preferred `status` over `phase`, the in-flight
window — and even the terminal `cancel_unavailable` state — silently fell
through to **Running**.

## User-facing change

* Clicking Cancel now actually deletes the labelled Kubernetes Jobs and
  flips the row to `cancelled` (= Failed in the dashboard taxonomy).
* While the Celery cancel task is in flight, the cluster card surfaces
  the row as **Pending** instead of **Running** so the user sees the
  action is being processed.
* If the cancel pipeline still fails (e.g. AKS API rejects the delete),
  the row immediately reads **Failed** instead of pretending to run.

## API / IaC diff summary

### Backend
* [api/services/state/repository.py](../../../api/services/state/repository.py)
  — add `_AZURE_TABLES_MAX_PAGE_SIZE = 1000` constant and `_clamp_page_size(limit)`
  helper; apply it in `list_children`, `list_active`, `list_completed`,
  `list_children_for_owner`, `get_history`, and `get_history_for_jobs` so
  every caller-supplied `limit` survives Azure Tables' page-size cap.
  Total entities returned are unaffected — the SDK iterator paginates
  transparently.

### Frontend
* [web/src/components/cards/ClusterBento/jobMapping.ts](../../../web/src/components/cards/ClusterBento/jobMapping.ts)
  — add `cancel_unavailable`, `cancel_blocked`,
  `cancel_retryable_failure` to the `FAILED` set; add a small `CANCELLING`
  set (`cancelling`, `Cancelling`) that maps to `Pending`; add an
  explicit guard in `classifyJobState` so `phase="cancelling"` wins over
  the legacy `status="running"` retry marker.

### Tests
* [api/tests/test_state_repo.py](../../../api/tests/test_state_repo.py)
  — `test_list_methods_clamp_page_size_to_azure_tables_max` asserts every
  caller-supplied `limit` over 1000 is clamped before reaching
  `TableClient.query_entities`.
* [web/src/components/cards/ClusterBento/jobMapping.test.ts](../../../web/src/components/cards/ClusterBento/jobMapping.test.ts)
  — `cancelling` → Pending and `cancel_unavailable` / `cancel_blocked` →
  Failed cases pinned.

No IaC changes. No sidecar layout changes.

## Validation evidence

* `uv run pytest -q api/tests/test_state_repo.py api/tests/test_blast_tasks.py api/tests/test_blast_jobs_routes.py`
  → **134 passed**.
* `npx vitest run src/components/cards/ClusterBento/jobMapping.test.ts`
  → **9 passed** (including the 3 new cancel-state cases).
* `uv run ruff check api/services/state/repository.py api/tests/test_state_repo.py`
  → **All checks passed!**
* Live Log Analytics workspace `log-elb-dashboard-3abp67bppeeg4`
  confirmed the `cancel` Celery task was failing repeatedly with
  `HttpResponseError(... ErrorCode:InvalidInput)`. The corresponding row
  for `98cf480b-bd0b-4339-a80a-1408433f016a` was inspected via
  `az storage entity query` and matched `phase=cancel_unavailable`,
  `status=failed`. The deployed `quick-deploy.sh api` will roll the fix
  out to revision `ca-elb-dashboard--0000005`.
