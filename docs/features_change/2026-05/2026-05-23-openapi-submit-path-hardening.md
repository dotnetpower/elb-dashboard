# OpenAPI BLAST submit path hardening — Phase 1+2

Date: 2026-05-23
Branch: main
Status: Implemented + tested (95/95 OpenAPI-related tests pass)

## Motivation

A deep review of the OpenAPI BLAST job request path (browser → api sidecar →
elb-openapi pod on AKS → ElasticBLAST K8s Jobs) surfaced ~20 concrete issues
in five categories: elb-openapi availability, token lifecycle, submit
reliability, cache/routing freshness, deploy-task accuracy, and operational
visibility.

The user's constraints for this change:

1. Existing dashboard features (token display/rotation, deploy, spec, proxy,
   jobs listing) must keep working with zero regression.
2. External API callers must keep using the existing public IP path
   (`http://<LB-IP>/api/v1/elastic-blast/…`) — that path stays alive
   indefinitely.
3. `OPENAPI_ALLOW_PUBLIC_LB=true` remains the production default.
4. K8s Secret-based token storage is out of scope for this PR (deferred).
5. TLS termination is also deferred: domain ownership has not been decided
   yet, and a domain is a hard prerequisite for any certificate
   (Let's Encrypt, self-signed, or otherwise). Hooks were planted so the
   day a domain is picked, flipping a single env var (`OPENAPI_PUBLIC_BASE_URL`)
   activates the HTTPS path without further code changes.

## User-facing change

* The user sees **no UI change**. Token display/rotation, BLAST Jobs list,
  the API Reference "Try it" proxy, the OpenAPI deploy panel, and external
  HTTP submits all behave identically to before.
* **First OpenAPI deploy** is materially more reliable: the deploy task no
  longer reports `succeeded` while the pod is actually broken
  (ImagePullBackOff, RBAC denial, no schedulable node), the resulting
  Deployment runs `replicas: 2` with a PDB + readiness/liveness probes, and
  failed Workload-Identity role assignments now fail the deploy fast with a
  clear error instead of letting it silently 403 on the first submit.
* **Token rotation** is no longer followed by a 30 s window of cached 401s
  on the BLAST Jobs page (cache invalidation is wired into the rotation
  call site).
* **Submit timeouts** under AKS API server pressure no longer leave orphan
  jobs: the per-call timeout is raised from 30 s to 90 s, and idempotent
  submits (those carrying an `idempotency_key` / `external_correlation_id`)
  transparently retry transport failures up to twice with exponential
  backoff. Non-idempotent submits fail fast on the first error, preserving
  the safe default.
* **Rate-limit** added on the OpenAPI submit surface: 2000 requests / 60 s
  sliding window, keyed by `X-ELB-API-Token` (with IP fallback). Dashboard
  polling and unrelated dashboard routes are unaffected.

## API/IaC diff summary

### Backend (api/)

* [api/tasks/openapi/manifests.py](../../../api/tasks/openapi/manifests.py)
  — Deployment now ships `replicas: 2`, RollingUpdate (`maxUnavailable: 0`,
  `maxSurge: 1`), readinessProbe + livenessProbe on `/healthz:8000`,
  `preStop sleep 10` + `terminationGracePeriodSeconds: 30` for graceful
  drain, and a `topologySpreadConstraints` rule that prefers different
  nodes (ScheduleAnyway so single-node blast pools still work). A new
  PodDisruptionBudget (`minAvailable: 1`) ships in the same manifest list.
* [api/tasks/openapi/deploy.py](../../../api/tasks/openapi/deploy.py) —
  After `wait_for_external_ip`, the task now waits up to ~120 s for at
  least one Ready replica (`k8s_get_deployment_ready_replicas`). When no
  replica reaches Ready, the task returns `status: failed` with a
  diagnostic message (ImagePullBackOff / CrashLoopBackOff / no schedulable
  node) instead of silently reporting `succeeded`. Success payload now
  includes `ready_replicas` and `desired_replicas`.
* [api/tasks/openapi/rbac.py](../../../api/tasks/openapi/rbac.py) —
  `assign_role_idempotent` returns a tuple `(ok, reason)`;
  `setup_workload_identity` raises `RuntimeError` when any role assignment
  genuinely fails (RoleAssignmentExists / Conflict still treated as
  success). The deploy task catches the exception and returns
  `status: failed` with the workload-identity payload, so the user never
  sees "deploy succeeded but the pod 403s".
* [api/services/k8s/monitoring.py](../../../api/services/k8s/monitoring.py)
  — New helper `k8s_get_deployment_ready_replicas` (returns
  `(ready_replicas, desired_replicas)`). Best-effort: returns `(0, 0)` on
  any error so the caller can decide the failure semantics.
* [api/services/openapi/token.py](../../../api/services/openapi/token.py)
  — `_sync_runtime_token` (called from both `get_openapi_api_token_status`
  and `ensure_openapi_api_token`) now flushes the external-jobs caches
  via `_reset_external_jobs_cache()`. Best-effort: never blocks the
  rotation call.
* [api/services/blast/external_jobs.py](../../../api/services/blast/external_jobs.py)
  — Two changes: (1) a new helper `_exception_is_transport_failure`
  classifies 503s by code (`openapi_unreachable`,
  `openapi_upstream_unreachable`); when a transport failure is detected,
  the openapi-client-kwargs IP cache is flushed and the negative-cache
  TTL drops to 5 s instead of the default 30 s. (2)
  `_openapi_client_kwargs_from_cluster` honours the new
  `OPENAPI_PUBLIC_BASE_URL` env (skipping the K8s IP lookup entirely when
  set); when the env is unset, behaviour is byte-identical to before.
* [api/services/external_blast.py](../../../api/services/external_blast.py)
  — `_DEFAULT_TIMEOUT_SECONDS` raised from 30 to 90. `submit_job` now
  retries transport-level failures (max 2 attempts, 0.5 s + 1.5 s backoff)
  but only when `payload` carries an `idempotency_key` or
  `external_correlation_id` so the sibling can dedupe a re-send.
  Non-idempotent calls fail fast on the first failure to avoid orphan
  jobs. Retry count is overrideable via `OPENAPI_SUBMIT_MAX_RETRIES` env.
* [api/services/openapi/runtime.py](../../../api/services/openapi/runtime.py)
  — New helper `get_public_tls_base_url()` reads `OPENAPI_PUBLIC_BASE_URL`
  and normalises trailing slashes. Empty when the env is unset.
* [api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py) — Both
  `/openapi/spec` and `/openapi/proxy` now check the public-TLS hook
  first; when set (and `https://…`) they bypass the `_is_private_ipv4`
  admin-token guard because TLS already encrypts the transit. When the
  env is unset, both routes run their legacy IP path with every existing
  safety check (`_public_lb_allowed`, `_is_private_ipv4`,
  `_OPENAPI_PROXY_ALLOWED_HEADERS`, `_enforce_openapi_proxy_target_path`).
* [api/app/openapi_rate_limit.py](../../../api/app/openapi_rate_limit.py)
  — New module. `OpenApiRateLimitMiddleware` enforces a per-key sliding
  window on `/api/v1/elastic-blast/*` and `/api/aks/openapi/proxy`. Key
  is `X-ELB-API-Token` (with `ip:` fallback). Default budget 2000 req /
  60 s, all knobs overrideable via env. 429 carries `Retry-After`.
  Wired into `create_app()` immediately after the request-id middleware.
* [api/main.py](../../../api/main.py) — `OpenApiRateLimitMiddleware`
  registered (one-line addition under the existing body-size guard).
* [api/conftest.py](../../../api/conftest.py) — Two test-only env
  defaults: `OPENAPI_SUBMIT_MAX_RETRIES=0` keeps retry-path tests
  millisecond-fast, and `reset_openapi_rate_limit_state()` is wired into
  the autouse cache-reset fixture.

### IaC / scripts

* None this round. The bicep + manifests changes for the future TLS
  rollout (nginx ingress, cert-manager, Ingress object) are deferred until
  the domain decision is made; the code-side hook (`OPENAPI_PUBLIC_BASE_URL`)
  is ready to consume them.

## Validation evidence

```
uv run pytest -q api/tests/test_openapi_task.py \
                 api/tests/test_external_blast_api.py \
                 api/tests/test_openapi_proxy_route.py \
                 api/tests/test_openapi_token.py \
                 api/tests/test_openapi_deployment.py \
                 api/tests/test_azure_tasks.py \
                 api/tests/test_openapi_rate_limit.py \
                 api/tests/test_openapi_tls_hook.py \
                 api/tests/test_route_contracts.py
# 95 passed in 107.26s
```

New tests added (11):

* `api/tests/test_openapi_task.py::test_build_manifests_hardens_for_ha` —
  asserts replicas=2, PDB, probes, RollingUpdate, topologySpread on
  every fresh build_manifests call.
* `api/tests/test_openapi_rate_limit.py` (6 tests) — middleware
  throttles the right paths, lets unrelated paths through, returns
  `Retry-After`, keys correctly by token then IP, can be disabled via
  env.
* `api/tests/test_openapi_tls_hook.py` (5 tests) — empty env is a strict
  no-op (legacy IP path runs unchanged), set env routes via the public
  base URL, `OPENAPI_PUBLIC_BASE_URL` is reachable independently of the
  K8s API surface, trailing slash is normalised.

## Out of scope (deferred)

These were considered in the same review but explicitly deferred per the
user's constraints; they are tracked as follow-ups:

1. K8s Secret-based token storage (#8). The current Deployment-env
   approach is preserved bit-for-bit.
2. TLS termination + nginx ingress + cert-manager. The code-side
   `OPENAPI_PUBLIC_BASE_URL` hook is ready; the K8s side (Ingress,
   cert-manager ClusterIssuer, helm installs) will land when a domain
   is chosen.
3. A "TLS status" card in the dashboard UI. Deferred (user choice).
4. Streaming-download integrity check (length / hash trailer) — kept on
   the 20-point review list for a separate change.
5. The `OPENAPI_ALLOW_PUBLIC_LB=true` env in the bicep template stays as
   the production default. It is now a strict subset of the new TLS
   hook: when the hook is set, the env's behaviour becomes irrelevant
   because the legacy IP branch is no longer reached.
