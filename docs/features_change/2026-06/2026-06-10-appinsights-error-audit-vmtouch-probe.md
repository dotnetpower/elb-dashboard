# App Insights 4xx/5xx audit + drop the dead `vmtouch-db-cache` 404 probe

## Motivation

Full sweep of the moonchoi production App Insights (`appi-elb-dashboard`,
`rg-elb-dashboard`) for all 4xx/5xx and exception telemetry over 14 days.

**Headline: the dashboard API served ZERO HTTP 4xx/5xx requests.** The
`requests` table has no client- or server-error status codes for the `elb-api`
role — the `_graceful` degrade-to-200 design on the monitor/data routes holds.
Every "error" in the App Insights Failures blade is an **outbound dependency
failure**, and all but one are benign by-design degradation:

| # | Dependency | 14d count | Root cause | Verdict |
|---|---|---|---|---|
| 1 | `GET …/daemonsets/vmtouch-db-cache` → **404** | 34 | `k8s_warmup_status` still probed a DaemonSet that was removed when warmup moved to a Job model (the in-pod vmtouch now runs inside the search pod). Result was 200-checked and never relied upon, but every warmup-status poll emitted a guaranteed 404. | **Fixed here** |
| 2 | `TableClient.get_entity` (code 0) | 20 | `repo.get(job_id)` `ResourceNotFoundError` for ids not in the Table (external-only / deleted job polls). Caught. | Benign |
| 3 | `Fetch GET /api/…` (code 0) | 13 | Aborted in-flight SPA fetches cancelled on navigation (TanStack Query), 1 each across distinct routes. | Benign |
| 4 | `ContainerClient.create_container` (0) | 3 | Container already exists (idempotent create). | Benign |
| 5 | AKS `GET /api/v1/nodes`, `…/jobs` (0) | 2 | Aborted/timeout during a cluster stop. | Transient |
| 6 | `requests.exceptions.ConnectionError` | 2 | Transient connect failure to a stopped/unreachable cluster (worker). | Transient |
| 7 | `BlobClient.download_blob` (0) | 1 | Blob not found. | Benign |

Only #1 is actionable: a guaranteed, repeating 404 that pollutes the Failures
blade and burns one parallel K8s round trip on every warmup-status poll.

## User-facing change

None visible. The dashboard's warmup status is unchanged: `vmtouch_ready` was
already always `0` in production (the probed DaemonSet never existed), and the
real node-side warmup DaemonSets are discovered via the separate
`app=db-warmup` label query. App Insights stops recording the spurious 404.

## API / IaC diff summary

- `api/services/k8s/warmup_status.py` — remove the `f_vmtouch` probe of
  `/apis/apps/v1/namespaces/default/daemonsets/vmtouch-db-cache` and its
  result handling. The response keeps `vmtouch_ready: 0` for backward
  compatibility (frontend `WarmupStatus` type + e2e/mocks still carry the
  field). One fewer parallel K8s GET per poll.
- `api/tests/test_k8s_warmup_status_parallel.py` — assert the dead probe is no
  longer issued (`vmtouch-db-cache` count `== 0`, `vmtouch_ready == 0`, `warm`
  still resolves from `create-workspace`).
- No IaC change.

## Validation evidence

- App Insights KQL (moonchoi `appi-elb-dashboard`, `az monitor app-insights
  query`): `requests | where resultCode matches regex '^[45][0-9][0-9]$'` → 0
  rows (14d); dependency-failure breakdown as tabled above.
- `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py
  api/tests/test_warmup_route.py api/tests/test_monitor_cache.py` → 46 passed.
- `uv run ruff check` clean.

## Note

This touches a K8s probe in `api/services/`, not sidecar layout or Bicep, so it
is validated by pytest only — no redeploy required to confirm correctness. The
404 stops the next time the worker/api image carrying this change is deployed.
