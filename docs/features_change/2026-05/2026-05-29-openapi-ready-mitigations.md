# 2026-05-29 — OpenAPI readiness hardening (post-PR1+PR2 mitigations)

## Motivation

PR1 (`/v1/ready` upstream probe + dashboard pre-flight integration) and
PR2 (AKS-managed PLS deploy guard) shipped on 2026-05-29. A self-audit
immediately after produced ten concrete robustness gaps spanning both the
sibling [dotnetpower/elastic-blast-azure](https://github.com/dotnetpower/elastic-blast-azure)
`elb-openapi` service and the elb-dashboard control plane. This change
closes all ten in one consolidated pass so the next on-call rotation
doesn't trip over them.

The gaps fell into four buckets:

1. **Sibling /v1/ready is too cheap to abuse** — no rate-limit, no
   token-budget cap, no metrics, no awareness of Cluster Autoscaler
   scale-from-zero, and the cluster name leaked in the public payload.
2. **Sibling had no test coverage for /v1/ready** — every probe-outcome
   branch was untested.
3. **Dashboard `ready()` could amplify outages** — no short-cache (every
   pre-flight retried the upstream probe), 4 s timeout too tight against
   the new 3 s upstream budget, no warning when the sibling is a stale
   version that returns 404 for /v1/ready.
4. **Submit gate never called `ready()`** — pre-flight UI showed the
   readiness card but the *internal* submit gate (the one a programmatic
   POST hits) had no openapi gate, so a script could submit while the
   cluster was demonstrably not ready.

Plus surface-level gaps: PLS transition state wasn't readable from the
dashboard, and the SPA had no friendly mapping for the new upstream
error codes.

## User-facing change

### For operators using the dashboard

- **Pre-flight readiness card is faster on repeat clicks.** The `ready()`
  result is cached in-process for ~5 s, so spamming "Re-check" no longer
  hammers the sibling.
- **Submit is blocked when openapi isn't ready, regardless of entry
  point.** The internal submit gate now includes an `openapi_ready` gate
  with the same upstream-code → action mapping the pre-flight card uses.
- **SPA toasts are actionable.** When a submit fails with
  `openapi_not_ready` or `openapi_unreachable`, the toast names the
  upstream cause (`no_workload_nodes`, `openapi_pod_not_ready`, …) and a
  one-line remediation. 429 rate-limit responses surface as "wait about
  a minute" instead of a generic 503.
- **PLS transition state is exposed.** New read-only route
  `GET /api/aks/openapi/pls` returns
  `{available, pls_enabled_env, pls_name, service_exists, service_has_pls_annotation, transition_pending, confirm_recreate_required}`
  so the SPA can render a "PLS state" cell without re-reading the
  Service manifest by hand. (SPA card wiring is a follow-up.)

### For operators using the sibling /v1/ready directly

- **Rate-limited at 30 req/min per auth-token bucket.** Over the limit
  returns 429 with `Retry-After: 60`.
- **Probe budget shortened to 2.5 s.** Each upstream `kubectl` shells out
  with `safe_exec(timeout=2.5)`.
- **Cluster name redacted by default.** Payload returns
  `"cluster_name": "sha256:<16-hex>"` unless
  `READY_MASK_CLUSTER_NAME=0`.
- **Cluster Autoscaler scale-from-zero is no longer a hard fail.** When
  the workload pool has zero Ready nodes *and* the
  `cluster-autoscaler-status` ConfigMap is present in `kube-system`, the
  probe degrades to `{status:"ok", degraded:"autoscaler_pending"}`
  instead of returning 503. Without the autoscaler ConfigMap the
  behaviour is unchanged — 503 with `code="no_workload_nodes"`.
- **New `/v1/ready/metrics` route.** Returns counters for each outcome
  code (`ok`, `k8s_unreachable`, `no_workload_nodes`,
  `workload_pool_check_failed`, `openapi_pod_not_ready`,
  `openapi_pod_check_failed`, `rate_limited`, `autoscaler_pending`)
  plus version and the active rate-limit. Auth-gated by the same
  `X-ELB-API-Token`.

### Sibling image version

`elb-openapi` VERSION bumped from `3.7.0` → `3.7.1`. Dashboard
`IMAGE_TAGS["elb-openapi"]` stays at `4.15` (tag scheme is independent
of upstream VERSION).

## API / IaC diff summary

### Sibling (`dotnetpower/elastic-blast-azure`)

- `docker-openapi/app/main.py`
  - `VERSION = "3.7.1"`, `READY_BUDGET_SECONDS = 2.5`.
  - New env knobs: `READY_AUTOSCALER_AWARE` (1), `READY_RATE_LIMIT_PER_MINUTE` (30), `READY_MASK_CLUSTER_NAME` (0).
  - Helpers: `_ready_token_bucket_check`, `_ready_record_metric`, `_ready_masked_cluster_name`, `_autoscaler_enabled_for_workload_pool`.
  - `/v1/ready` accepts `X-ELB-API-Token`, applies rate-limit, increments metric counters, masks cluster name, degrades to `degraded="autoscaler_pending"` when applicable.
  - New `/v1/ready/metrics` route.
- `docker-openapi/tests/` (new) — `conftest.py` (env pinning + sys.path), `test_ready.py` (10 tests covering every probe-outcome and rate-limit branch), `README.md`, `requirements-dev.txt`.

### Dashboard (this repo)

- `api/services/external_blast.py`
  - `_READY_TIMEOUT_SECONDS` 4.0 → 5.0 (env override).
  - New `_READY_CACHE_TTL_SECONDS` (5.0) + `_READY_CACHE` keyed by `(base_url, sha256(token)[:8])`. Cached entries store either a success dict or the `HTTPException` itself so the second caller re-raises the original.
  - `ready()` now: cache lookup → handle 404 (stale sibling, WARN with `event="ready_probe_stale_sibling"`, return `{ready:True, skipped:"version_mismatch"}`) → 429 (`openapi_ready_rate_limited` with `limit_per_minute`) → 503 (`openapi_not_ready` with structured detail) → 200 (success).
  - Public `reset_ready_cache()` for tests.
- `api/services/blast/submit_gates.py`
  - New `_gate_openapi_ready` and `_openapi_action_for_code` helpers.
  - `evaluate_submit_gates` runs `_gate_openapi_ready()` between `aks_cluster` and `blast_database`.
  - Gracefully no-ops when `_base_url()` raises (= openapi not configured).
- `api/services/openapi/pls_status.py` (new) — `PlsStatus` dataclass + `get_pls_status` probe that reads the live Service annotations via `_get_k8s_session`, returns `transition_pending=True` when env says PLS is on but the Service is missing `azure-pls-create`.
- `api/routes/aks/openapi.py` — new `GET /api/aks/openapi/pls` route.
- `api/routes/aks/__init__.py` — re-export `aks_openapi_pls`.
- `web/src/api/client.ts` — `formatApiError` now handles `openapi_not_ready`, `openapi_unreachable`, `openapi_ready_rate_limited` with `OPENAPI_UPSTREAM_HINTS` lookup keyed by `upstream_code`.

### Tests

- Backend new:
  - `api/tests/test_openapi_pls_status.py` (7 tests).
  - `test_external_blast_api.py` — 2 new tests for cache + 429.
  - `test_blast_submit_gates.py` — 4 new tests for `_gate_openapi_ready`.
  - `test_route_contracts.py` — `/api/aks/openapi/pls` precedence.
- Frontend new: `web/src/api/client.test.ts` (6 tests).
- Sibling new: `docker-openapi/tests/test_ready.py` (10 tests).

## Validation evidence

```
$ cd /home/moonchoi/dev/elastic-blast-azure/docker-openapi && python -m pytest tests/ -q
10 passed, 22 warnings in 1.37s

$ uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_openapi_pls_status.py \
    api/tests/test_blast_submit_gates.py api/tests/test_route_contracts.py \
    api/tests/test_openapi_pls_deploy_guard.py
97 passed in 7.31s

$ uv run pytest -q api/tests
1859 passed, 3 skipped in 50.30s

$ uv run ruff check api/services/external_blast.py api/services/blast/submit_gates.py \
    api/services/openapi/pls_status.py api/routes/aks/openapi.py api/routes/aks/__init__.py \
    api/tests/test_external_blast_api.py api/tests/test_blast_submit_gates.py \
    api/tests/test_openapi_pls_status.py api/tests/test_route_contracts.py
All checks passed!

$ cd web && npm run build
✓ built in 8.01s

$ cd web && npx vitest run --reporter=basic
Test Files  54 passed (54)
     Tests  421 passed (421)
```

## Self-review

- **Consumer search.** Every modified symbol was searched:
  `external_blast.ready` / `external_blast.reset_ready_cache` / `_gate_openapi_ready` / `aks_openapi_pls`. Existing callers (`pre_flight`, `evaluate_submit_gates`, `test_route_contracts`, `aks/__init__.py`) updated where needed.
- **Backward compat.** `ready()` return shape extended with optional `skipped` / `version` fields on 404; existing 200 callers see no change. New gate is auto-skipped when openapi is not configured, so existing test fixtures don't need the env knob.
- **Wide sweep.** Full `api/tests` (1859) + full `web/` vitest (421) + sibling `tests/` (10) all green.
- **Lint + build.** `ruff check` clean on all touched paths; `npm run build` succeeds; no new TS warnings.
- **Diff audit.** Touched files match the plan; no incidental changes to unrelated routes / tasks.
- **Fixture parity.** `_stub_all_ok` in `test_blast_submit_gates.py` updated for the new gate ID set; `web/src/mocks/**` carry no openapi error fixtures so no SPA mock drift.

## Follow-ups (not in this change)

- SPA: render a PLS state cell on the OpenAPI / AKS card consuming `GET /api/aks/openapi/pls`.
- Sibling: optionally export `/v1/ready/metrics` to Prometheus or App Insights instead of leaving it as a JSON snapshot.
- Sibling: per-tenant rate-limit instead of per-token (multiple tokens per tenant can each get 30/min).
