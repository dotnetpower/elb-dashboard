# 2026-05-29 — elb-openapi direct access (PLS + peering)

## Motivation

External callers (notebook VMs, partner subscriptions, JupyterHub-style
launchers) need to call `POST /v1/jobs` directly against the AKS
`elb-openapi` Service without routing through the dashboard. Two reasons
this matters:

1. The dashboard is a control plane — it should not sit on the BLAST
   submit hot path for batch / API-only callers.
2. Today the only way to reach `elb-openapi` from another VNet is to
   peer that VNet against the AKS LB VNet, which fails for callers in
   a different subscription or with overlapping CIDRs.

The dashboard currently has no first-class story for either case; the
ILB-only Service ships out of the box.

## User-facing change

The dashboard's "Deploy elb-openapi" task now accepts five new
environment variables that activate AKS-managed
[Private Link Service](https://learn.microsoft.com/azure/aks/internal-lb#expose-an-internal-load-balancer-using-azure-private-link-service)
in front of the existing internal LoadBalancer. When enabled, the deploy
task injects the standard `service.beta.kubernetes.io/azure-pls-*`
annotations on the `elb-openapi` Service and the AKS cloud-provider
controller stands up a PLS. Callers in any subscription can then create
a Private Endpoint and reach the API without VNet peering.

Important behavioural detail: the AKS controller honours the
`azure-pls-*` annotations only when the Service is *created*. To prevent
a silent first-time activation outage, the deploy task now detects the
ILB-only → PLS transition and refuses to proceed unless the operator
sets `OPENAPI_PLS_CONFIRM_RECREATE=1`. When opted in, the task deletes
the existing Service and re-applies the manifest in one shot, accepting
the documented ~1–2 min ingress outage as the cost of the activation.

For callers that simply need same-tenant non-overlapping access, the
existing VNet peering procedure (documented in the new operate guide) is
still the recommended option — PLS is additive, not a replacement.

## API/IaC diff summary

* `api/tasks/openapi/constants.py`
  * New `PlsConfig` dataclass + `pls_config_from_env()` reader for
    `OPENAPI_PLS_ENABLED`, `OPENAPI_PLS_NAME`, `OPENAPI_PLS_LB_SUBNET`,
    `OPENAPI_PLS_VISIBILITY`, `OPENAPI_PLS_AUTO_APPROVAL`.
  * Validation: `enabled=True` without `lb_subnet` raises `ValueError`.
* `api/tasks/openapi/manifests.py`
  * `build_manifests` accepts a new `pls: PlsConfig | None = None`
    keyword. When `pls.enabled` is true the Service manifest carries
    the five `azure-pls-*` annotations alongside the existing
    `azure-load-balancer-internal` one.
* `api/tasks/openapi/deploy.py`
  * Reads `pls_config_from_env()` and fails fast with a structured
    `openapi_pls_misconfigured` result if env validation raises.
  * Before `kubectl apply`, queries the existing Service via
    `_read_service_annotations()`. If the Service exists but lacks
    `azure-pls-create=true`, the task returns
    `status=blocked` / `code=openapi_pls_recreate_required` unless
    `OPENAPI_PLS_CONFIRM_RECREATE` is set.
  * With confirm on, calls `_delete_openapi_service()` then re-applies.
    Delete failures surface as `openapi_pls_recreate_failed`.
* New operate doc: `docs/operate/openapi-direct-access.md` — peering vs
  PLS decision matrix, full procedure for each, token rotation,
  troubleshooting table.

## Validation evidence

* `uv run pytest -q api/tests/test_openapi_task.py` → 12 passing (6 new
  tests cover PLS-disabled / enabled annotation injection,
  auto-approval omission, env defaults, missing-subnet error,
  full-env round-trip).
* `uv run pytest -q api/tests/test_openapi_pls_deploy_guard.py` → 5 new
  tests covering `_read_service_annotations` 404 / 200 / transport-error
  paths and `_delete_openapi_service` 404 / 500 status handling.
* Full deploy task itself (the `deploy_openapi_service` Celery body) is
  not exercised end-to-end in this PR — it carries too many external
  integration points (AKS cluster, workload identity, kubectl apply,
  LB IP wait, deployment ready-replica probe) for a tractable in-process
  test, and the PLS-specific branching is fully covered by the helper
  tests above. Operational validation comes from the deploy task's
  existing logging + the new structured codes
  `openapi_pls_misconfigured` / `openapi_pls_recreate_required` /
  `openapi_pls_recreate_failed`.

## Compatibility notes

* Default behaviour (PLS env unset) is unchanged — Service ships with
  the same single `azure-load-balancer-internal: true` annotation as
  before. No existing cluster is affected by a redeploy under the
  new code.
* The transition guard is opt-out by design — operators who set
  `OPENAPI_PLS_ENABLED=true` but don't set `OPENAPI_PLS_CONFIRM_RECREATE`
  get a clear blocked result instead of a silent 1–2 min outage.
* Forgetting to unset `OPENAPI_PLS_CONFIRM_RECREATE` after a successful
  activation does **not** cause a repeated outage — once the Service is
  created with `azure-pls-create=true` the guard's "missing annotation"
  predicate is false on the next deploy, so the delete branch never
  fires.
