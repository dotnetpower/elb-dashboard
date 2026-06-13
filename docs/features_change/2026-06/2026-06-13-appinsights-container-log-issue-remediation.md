# App Insights / container-log issue remediation (2026-06-13)

## Motivation

A severity-ranked audit of App Insights (`appi-elb-dashboard`) and the
`ca-elb-dashboard` Container App console logs (`ContainerAppConsoleLogs_CL`)
surfaced four actionable defects. App Insights itself held **zero telemetry for
30 days**, so the audit relied on Log Analytics container logs.

## Findings and remediation

| # | Severity | Finding | Root cause | Fix |
|---|----------|---------|------------|-----|
| 1 | HIGH | `deploy_openapi_service` raised `RuntimeError("acr_resource_group is required …")` on every SPA-triggered deploy (10x). | The `/aks/openapi/deploy` route forwarded `body["acr_resource_group"]` verbatim; a SPA build that saved a config without the ACR RG queued a task guaranteed to raise. | Route now resolves the ACR RG with the same precedence as the auto-deploy path: body → `PLATFORM_ACR_RESOURCE_GROUP` → `AZURE_RESOURCE_GROUP`. |
| 2 | HIGH | `worker` sidecar OOM-killed (SIGKILL signal 9 → `WorkerLostError`), ongoing in the last 24h. | **Deployment drift** — the repo (Bicep + compiled `main.json`) already pins `worker` at `1.0 vCPU / 2.0Gi`, but the live revision still runs the stale `0.5 vCPU / 1.0Gi` because `quick-deploy.sh` only patches container images, never resources. | No code change — a full Bicep redeploy applies the already-committed `1.0/2.0`. |
| 4 | MEDIUM | 58x `requests.exceptions.HTTPError: 503 … /apis/metrics.k8s.io` tracebacks in the `api` log. | metrics-server returns 503 (APIService unhealthy) / 404 (not installed) on freshly started or scaling AKS; `k8s_top_nodes` / `k8s_top_pods` called `raise_for_status()` and the traceback flooded the log even though the route degrades correctly. | Both helpers now treat a 503/404 from `metrics.k8s.io` as "metrics unavailable" → return an empty list and log at DEBUG, no raise. |
| 6 | MEDIUM | App Insights received **zero** telemetry for 30 days even though the resource exists and is healthy. | `postprovision.sh` set the sidecar `APPLICATIONINSIGHTS_CONNECTION_STRING` from `${APPLICATIONINSIGHTS_CONNECTION_STRING:-}`, which is empty whenever the shell env is unset (the common standalone-run case). The Container App baked an empty value → telemetry off. | `postprovision.sh` now resolves the connection string from shell env → `azd env get-values` → the live App Insights component in the platform RG, looked up via the **generic ARM provider** (`az resource list/show`). Empty is still acceptable (telemetry off, zero cost) when no component exists. |

### Capacity-signals interaction (verified safe)

`k8s_top_nodes` previously raised on a metrics-server 503, so
`api/services/blast/capacity_signals.py::_safe_top_nodes` returned `None`
("degraded"); it now returns `[]`. This only changes the informational
`signals_degraded` flag in the capacity route response and the deny *reason*
string — the gate **decision** is unchanged: `evaluate_capacity_gate` drives its
tree from `pressure` (core API `/api/v1/pods`, independent of metrics-server),
pending pods, watermarks and slots, and `_pool_headroom(top_nodes)` yields
`(0, 0)` headroom for both `None` and `[]` (deny + retryable either way).
`BLAST_GATE_ENABLED` is also `false` in production, so the gate is inert.

### Benign / no-change (documented)

- **beat shelve `CRITICAL: invalid operation on closed shelf`** — emitted only
  during SIGTERM shutdown (revision flip) by Celery's `PersistentScheduler`;
  schedule state is rebuilt on start. Cosmetic; not fixed to avoid churn.
- **AKS `OperationNotAllowed` (start on running cluster), VMSS
  `InvalidPolicyParameters`, transient redis blips, normal SIGTERM** — benign
  idempotent calls / self-recovering / expected lifecycle.
- **Deleted-cluster DNS errors (`NameResolutionError`, `ControlPlaneNotFound`)**
  — the cluster-health gate already skips `exists=False` clusters; residual
  noise is from clusters that exist in ARM but whose API server FQDN is
  transiently unresolvable (degrade-open is correct).

## API / IaC diff summary

- `api/routes/aks/openapi.py` — `aks_openapi_deploy` resolves `acr_resource_group`
  fallback before enqueue. No response-shape change.
- `api/services/k8s/metrics.py` — `k8s_top_nodes` / `k8s_top_pods` short-circuit
  to `[]` on metrics-server 503/404 (`getattr`-guarded so fakes without a
  `status_code` are unaffected).
- `scripts/dev/postprovision.sh` — multi-source resolution of
  `APPLICATIONINSIGHTS_CONNECTION_STRING_VAL`. The App Insights fallback uses
  the generic ARM provider (`az resource list/show`) rather than
  `az monitor app-insights component show` — the latter needs the
  `application-insights` CLI extension and HANGS a non-interactive `azd up`
  when the extension is absent and the auto-install prompt blocks on stdin
  (a `|| true` cannot rescue a hung process).

No Bicep change in this commit (the worker `1.0/2.0` bump is already committed in
`infra/modules/containerAppControl.bicep` / `infra/main.json`).

## Validation evidence

- `uv run ruff check …` — clean on all changed files.
- `uv run pytest -q api/tests` — **3366 passed, 3 skipped**.
- New regression tests:
  - `test_openapi_deploy_route_falls_back_to_platform_acr_rg_env`
  - `test_openapi_deploy_route_falls_back_to_azure_resource_group_env`
  - `test_k8s_top_pods_returns_empty_when_metrics_server_unavailable`
  - `test_top_nodes_returns_empty_when_metrics_server_unavailable`
- `bash -n scripts/dev/postprovision.sh` — syntax OK.

## Deployment note

Findings #1, #2, #4, #6 only take effect after a **full Bicep redeploy** of the
Container App control plane (image-only `quick-deploy.sh` will not apply the
worker resource bump or the telemetry env). The redeploy rebuilds the `api`
image (carries #1, #4), applies worker `1.0/2.0` (#2), and wires the App
Insights connection string (#6).
