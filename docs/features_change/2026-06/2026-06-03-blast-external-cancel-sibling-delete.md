# Cancel external (OpenAPI sibling) BLAST jobs via the sibling DELETE endpoint

## Motivation

Cancelling a BLAST job that originated from the OpenAPI sibling service failed
with `cancel_unavailable`. External jobs run on the **sibling's own AKS
cluster** (`elb-cluster-02` in `rg-elb-cluster`), but the dashboard cancel route
always dispatched the direct Kubernetes cancel task. When the cancel request
carried no cluster coordinates, the SPA filled in a hardcoded `"elb-cluster"`
fallback, so the worker tried to reach `Microsoft.ContainerService/managedClusters/elb-cluster`
under `rg-elb-dashboard` — a cluster that does not exist — and retried until it
gave up with:

```
ResourceNotFoundError("(ResourceNotFound) The Resource
'Microsoft.ContainerService/managedClusters/elb-cluster' under resource group
'rg-elb-dashboard' was not found ...")
status='failed' phase='cancel_unavailable'
```

The dashboard cannot (and must not) guess the sibling's cluster coordinates. The
sibling already exposes a `DELETE /v1/jobs/{job_id}` endpoint that cancels the
run and tears down the K8s resources using its in-cluster kubeconfig.

## User-facing change

* Cancelling an external/OpenAPI job now succeeds: the request is routed to the
  sibling's `DELETE /v1/jobs/{job_id}`, the run is stopped on the sibling's
  cluster, and the local job row is flipped to `cancelled` so the SPA reflects
  the change immediately.
* Dashboard-owned jobs are unchanged — they still use the in-cluster Kubernetes
  cancel task with the coordinates stored on the job row.
* If the sibling is unreachable, the cancel surfaces a clean `503
  openapi_unreachable` instead of a misleading `cancel_unavailable`.

## API / IaC diff summary

* `api/services/external_blast.py` — added `delete_job(job_id, *, base_url=None,
  api_token=None)` that calls the sibling `DELETE /v1/jobs/{id}`, mirroring the
  `get_job` error handling (`_raise_upstream_error` for HTTP status errors,
  `503 openapi_unreachable` for transport failures).
* `api/routes/blast/jobs.py` — `POST /api/blast/jobs/{job_id}/cancel` now detects
  external rows (`payload.external` dict or `owner_upn == "api"`) and routes them
  to the new `_cancel_external_job` helper; dashboard jobs continue through the
  k8s cancel task. The sibling's `HTTPException` is re-raised verbatim; any other
  failure becomes `502 external_cancel_failed`.
* `web/src/pages/blastResults/blastJobScope.ts` — removed the hardcoded
  `"elb-cluster"` last-resort fallback for `clusterName` (now `""`), so a missing
  cluster can never trigger a wrong-cluster cancel. External jobs are cancelled
  via the sibling, which owns its cluster.
* No IaC changes.

## Validation evidence

* `uv run pytest -q api/tests/test_blast_jobs_routes.py api/tests/test_external_blast_api.py` — 71 passed.
  New tests:
  * `test_external_blast_delete_job_calls_v1_endpoint`
  * `test_external_blast_delete_job_transport_error_is_503`
  * `test_blast_job_cancel_external_routes_to_sibling_delete`
  * `test_blast_job_cancel_dashboard_uses_k8s_task`
  * `test_blast_job_cancel_external_sibling_unreachable_returns_503`
* `uv run pytest -q api/tests` — 2518 passed, 3 skipped (one unrelated flake in
  `test_terminal_exec.py::test_run_truncates_stdout_above_cap`, which passes in
  isolation — a parallel subprocess-timing flake, not caused by this change).
* `uv run ruff check api` — all checks passed.
* `cd web && npx vitest run src/pages/blastResults/blastJobScope.test.ts` — 6 passed.
* `cd web && npm run build` — succeeded.
</content>
</invoke>
