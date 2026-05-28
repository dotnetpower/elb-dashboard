# 2026-05-29 — elb-openapi /v1/ready probe + external submit pre-flight

## Motivation

`POST /api/v1/elastic-blast/submit` (the external API facade) and `POST /v1/jobs`
(the sibling OpenAPI direct path) both spend the full ~90 s submit timeout
waiting on a `httpx` response when the underlying AKS cluster is stopped or
the `elb-openapi` pod is down. The caller eventually sees an opaque
`openapi_unreachable`. The existing `/v1/health` is too heavy for a
pre-flight check (≈200–800 ms cost: `DefaultAzureCredential.get_token` +
`kubectl get nodes`) and does not verify the BLAST workload node pool, so a
system-pool-only cluster reports healthy while every submit ends up stuck
in `Pending`.

## User-facing change

* New sibling endpoint `GET /v1/ready` (auth-gated like every other `/v1/*`).
  Returns 200 with `{"ready": true, "checks": {...}, "version": "3.7.0",
  "cluster_name": ..., "timestamp": ..., "budget_seconds": 3.0}` only when
  all three probes pass:
  * `k8s_api`       — `kubectl get --raw /readyz --request-timeout=1s`
  * `workload_pool` — at least one Ready node matches
    `ELB_OPENAPI_WORKLOAD_POOL_LABEL` (default `workload=blast`, set empty
    to skip the check on autoscale-only clusters)
  * `openapi_pod`   — `elb-openapi` Deployment has `readyReplicas >= 1`

  Otherwise returns 503 with `{"ready": false, "code": <upstream_code>,
  "message": ..., "checks": {...}}` where `code` is one of
  `k8s_unreachable` / `no_workload_nodes` / `workload_pool_check_failed` /
  `openapi_pod_not_ready` / `openapi_pod_check_failed`. The endpoint
  intentionally avoids `DefaultAzureCredential` so an AKS-stopped scenario
  surfaces as a transport timeout, never a 30 s ARM hang.
* Dashboard `external_blast.ready()` client wraps the probe with a 4 s
  timeout (1 s slack over the sibling's hard budget). On 404 it
  fails open (older sibling images without `/v1/ready`) so submits keep
  working during the cross-repo rollout. On 503 it raises
  `HTTPException(503, detail={code: "openapi_not_ready", upstream_code,
  message, checks})`; on transport errors it raises
  `HTTPException(503, detail={code: "openapi_unreachable", probe:
  "ready", message})`.
* `submit_external_blast_job` (in `api/routes/elastic_blast.py`) now calls
  `external_blast.ready()` immediately before `external_blast.submit_job(...)`
  so the caller gets a precise, actionable error before the long submit
  timeout fires.

## API/IaC diff summary

* `elastic-blast-azure/docker-openapi/app/main.py`
  * `VERSION = "3.6.0"` → `"3.7.0"`
  * New env vars `ELB_OPENAPI_READY_BUDGET_SECONDS` (default 3.0) and
    `ELB_OPENAPI_WORKLOAD_POOL_LABEL` (default `workload=blast`)
  * New `@v1.get("/ready")` route
* `api/services/external_blast.py`
  * New constant `_READY_TIMEOUT_SECONDS` (`OPENAPI_READY_TIMEOUT_SECONDS`,
    default `4.0`)
  * New function `ready(*, base_url=None, api_token=None) -> dict`
* `api/routes/elastic_blast.py`
  * `submit_external_blast_job` calls `external_blast.ready()` before
    `external_blast.submit_job(...)`
* `api/services/image_tags.py`
  * `"elb-openapi": "4.14"` → `"4.15"` (tracks sibling 3.6.0 → 3.7.0).
    Mapping comment updated.

The dashboard's internal `/api/blast/submit` is **not** changed: that path
does not go through `elb-openapi` (it talks to the terminal sidecar +
`kubectl` directly), so an openapi-pod readiness gate would be incorrect
for it. The existing `_gate_aks_cluster` already blocks the only relevant
case (AKS stopped) for the internal submit.

## Validation evidence

* `uv run pytest -q api/tests/test_external_blast_api.py -k "ready or
  submit_aborts or submit_proceeds"` → 6 new tests pass:
  * `test_external_blast_ready_returns_payload_on_200`
  * `test_external_blast_ready_503_surfaces_upstream_code`
  * `test_external_blast_ready_transport_error_is_openapi_unreachable`
  * `test_external_blast_ready_404_fails_open`
  * `test_external_blast_submit_aborts_when_ready_blocks`
  * `test_external_blast_submit_proceeds_when_ready_ok`
* `uv run pytest -q api/tests` (full suite) — see end-of-PR validation.
* Sibling repo: syntax-validated via `python -c "import ast; ast.parse(...)"`
  on the modified `docker-openapi/app/main.py`. Live behaviour will be
  validated by the sibling repo's own deploy + smoke flow during the
  next image rebuild.

## Compatibility notes

* Old sibling image (≤ 3.6.0 / dashboard tag ≤ 4.14): dashboard client
  receives 404 → fails open → submit proceeds as before. No regression for
  clusters running yesterday's image.
* New sibling image (3.7.0 / dashboard tag 4.15): dashboard client receives
  structured 503s → external-facade callers get an actionable
  `upstream_code` instead of waiting 90 s for `openapi_unreachable`.
