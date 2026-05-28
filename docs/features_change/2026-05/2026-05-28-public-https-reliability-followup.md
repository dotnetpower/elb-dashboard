# Public-HTTPS reliability + diagnostics + multi-cluster UX (2026-05-28, follow-up)

## Motivation

After the 2026-05-28 incident the operator triage took ~30 minutes
because the failure mode was invisible to the SPA (the Redis cache went
cold after a revision restart, the SPA banner showed only "Task failed
(phase=wait_certificate_ready)", and the running task progress was
forgotten when the operator switched away from the Public HTTPS tab).
This change ships ten follow-up improvements grouped into three waves
(A operability, B safety, C polish).

## User-facing change

* **Public HTTPS status survives revision restarts.** The Redis-only
  cache is now backed by a durable Storage Table singleton, and a new
  beat task (`api.tasks.openapi.reconcile_public_https`, default every
  120s) refreshes the hot cache from the singleton + the live
  Certificate state. `cert_expires_at` is now populated automatically.
* **Setup failure surfaces the real root cause.** Failed
  `setup_openapi_public_https` tasks now ship a `diagnostics` field
  collected from Certificate / Order / Challenge / solver-Pod state
  (`certificate.condition`, `order.state`, `challenge.reason`,
  `challenge.solverpod`). The SPA renders the multi-line digest under
  the error banner so the operator can tell "wrong status code '503'"
  apart from "invalidContact" without `kubectl describe`.
* **Cross-subscription deploy guard.** `quick-deploy.sh` now hard-fails
  with a red ERROR when the active `az login` subscription differs from
  the `azd env` subscription, requiring `ELB_ALLOW_SUB_MISMATCH=1` to
  proceed. Stops the "deployed to the wrong cluster" class of mistake
  that happened on 2026-05-28.
* **SPA Operator-email validator stays in sync with the backend.** The
  panel fetches `/api/aks/openapi/public-https/operator-email-rules`
  on mount and unions the server's `private_use_tlds` list with the
  hard-coded fallback. Adding a TLD to `_PRIVATE_USE_TLDS` on the
  backend now propagates to the SPA without a new build.
* **Public HTTPS progress survives tab switches across multiple
  clusters.** localStorage key is now `elb.publicHttps.runningTask.v2.<cluster>`
  with a legacy `v1` migrator. Two clusters can be enabled in parallel
  without each other's progress badge being clobbered.
* **Systempool capacity early warning.** New `/api/monitor/aks/node-pressure`
  reports per-pool CPU/memory request % with a 90% warning flag. SPA
  hookup will land in a follow-up PR.

## API / IaC diff summary

* `api/services/state/singletons.py` (new) — generic
  `save_singleton` / `load_singleton` / `clear_singleton` backed by an
  Azure Table `dashboardsingletons`. Endpoint env: `AZURE_TABLE_ENDPOINT`.
* `api/services/openapi/runtime.py` — `save_openapi_public_base_url`
  now dual-writes (durable + Redis); `get_openapi_public_base_url`
  reads Redis first then falls back to the durable singleton and
  re-populates Redis opportunistically; `clear_openapi_public_base_url`
  clears both tiers.
* `api/tasks/openapi/reconcile_public_https.py` (new) — Celery beat
  task `api.tasks.openapi.reconcile_public_https`. Beat schedule:
  120s (env `CELERY_BEAT_OPENAPI_PUBLIC_HTTPS_SECONDS`).
* `api/tasks/openapi/public_https.py` — new
  `_collect_cert_issuance_diagnostics(...)` helper invoked from
  `_wait_for_certificate_ready` AND the pipeline-level `except`; the
  failed task result now carries `diagnostics` (≤2000 chars).
* `api/services/k8s/ingress.py` — `build_cluster_issuer` now uses
  `workload_pool_pod_template()` so future ACME Issuers reuse the
  same blastpool-friendly podTemplate without copy-paste. Legacy
  `SYSTEM_POOL_*` / `*_for_system_pool` aliases removed in the same
  change; the canonical names are `WORKLOAD_POOL_*` /
  `patch_manifest_for_workload_pool` / `fetch_install_manifest_for_workload_pool`.
* `api/routes/aks/openapi.py` — new `GET /api/aks/openapi/public-https/operator-email-rules`
  exposes the validator rules (private TLDs, regex, max length) for the
  SPA to mirror.
* `api/routes/monitor/aks.py` — new `GET /api/monitor/aks/node-pressure`
  using `api/services/k8s/node_pressure.py::k8s_node_request_pressure`.
* `scripts/dev/az-context.sh` — `prepare_deploy_env_from_az_login`
  fails fast (exit 2) on sub mismatch unless
  `ELB_ALLOW_SUB_MISMATCH=1` is set.
* `web/src/components/SettingsPanel.tsx` — multi-cluster localStorage
  (`v2.<cluster>`), server-synced private-TLD set, diagnostics
  surfacing under the error banner, `StatusLine` now uses
  `white-space: pre-wrap` so multi-line errors render correctly.
* `web/src/api/aks.ts` — new `openApiOperatorEmailRules()` typed client.

No Bicep changes (the durable table is created lazily on first write;
`AZURE_TABLE_ENDPOINT` is already injected on the api / worker sidecars).

## Validation evidence

* `uv run pytest -q api/tests` → **1758 passed, 3 skipped** (was 1562
  before; +196 new tests across state singletons, reconcile,
  node-pressure, cert-manager challenge, ingress patch rename).
* `uv run ruff check api/...` for every changed module → All checks
  passed.
* `cd web && npm run build` → built in 9.52s, no errors.
* `cd web && npx vitest run` → **53 files / 394 tests passed**.

## Files touched (this follow-up only)

```
api/services/state/singletons.py            (new)
api/services/openapi/runtime.py
api/services/k8s/ingress.py
api/services/k8s/node_pressure.py           (new)
api/tasks/openapi/__init__.py
api/tasks/openapi/public_https.py
api/tasks/openapi/reconcile_public_https.py (new)
api/celery_app.py
api/routes/monitor/aks.py
api/routes/aks/openapi.py
api/tests/test_state_singletons.py                 (new)
api/tests/test_openapi_public_https_reconcile.py   (new)
api/tests/test_k8s_node_pressure.py                (new)
api/tests/test_cert_manager_challenge_path.py      (new)
api/tests/test_openapi_public_https.py             (rename + assertions)
scripts/dev/az-context.sh
web/src/components/SettingsPanel.tsx
web/src/api/aks.ts
```

## Follow-up parked

* SPA card consuming `/api/monitor/aks/node-pressure` (UI design TBD).
* `cert-manager`-level admission policy that injects the workload-pool
  podTemplate on every solver Pod (Kyverno or webhook) — would let us
  drop the per-Issuer podTemplate plumbing entirely.
