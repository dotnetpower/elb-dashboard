# Public HTTPS hardening + Auto OpenAPI deploy (2026-05-28)

## Motivation

On `elb-cluster-01` the Public HTTPS panel showed `NOT EXPOSED` for hours
even though the operator clicked Enable. Live triage surfaced three
independent root causes that the dashboard either could not surface or
silently absorbed:

1. `_FALLBACK_OPERATOR_EMAIL = "noreply@elb-dashboard.local"` failed
   Let's Encrypt ACME registration with
   `urn:ietf:params:acme:error:invalidContact` ("Domain name does not
   end with a valid public suffix (TLD)") so the whole pipeline
   silently failed at Step 6.
2. The `ingress-nginx-controller` Pod sat in `Pending` for 15h with
   `0/3 nodes are available: 1 Insufficient cpu, 2 node(s) had
   untolerated taint(s)` because the `systempool` patch landed it on a
   single DS2_v2 node that AKS system add-ons had already requested to
   99 %.
3. The `elb-openapi` namespace did not exist at all — the OpenAPI
   Service is not deployed automatically when AKS starts, so a
   dashboard-driven AKS stop/start cycle leaves the API surface dark
   until an operator clicks Deploy again.

## User-facing change

* **Settings → Public HTTPS** now auto-fills `Operator email` from the
  signed-in caller (`/api/me` `upn`, with MSAL `account.username` as a
  fallback). The Enable button is disabled when the email is empty or
  uses a private-use TLD that Let's Encrypt will reject
  (`.local`, `.localhost`, `.internal`, `.test`, `.example`, …). The
  hint line explains the rejection inline so operators can fix it
  without reading server logs.
* **AKS Start** (lifecycle) and **AKS Provision** both now enqueue
  `deploy_openapi_service` automatically. Existing explicit
  `auto_openapi` payloads still win; absent ones fall back to
  `PLATFORM_ACR_NAME` / `STORAGE_ACCOUNT_NAME` / `AZURE_RESOURCE_GROUP`
  / `AZURE_TENANT_ID` env defaults that are already injected on the
  api / worker sidecars. Opt-out: set
  `ELB_AUTO_OPENAPI_DEPLOY=false` on the Container App revision.
* **ingress-nginx + cert-manager** now schedule on the **blastpool**
  (user-mode) instead of the **systempool**. The toleration switches
  from `CriticalAddonsOnly:Exists` to `workload=blast:NoSchedule` and
  the `nodeSelector` switches from `mode=system` to `mode=user` so the
  starved systempool is no longer in the path.

## API / IaC diff summary

* `POST /api/aks/openapi/public-https` — adds `_validate_operator_email`
  defence-in-depth: 400 when empty / malformed / private-TLD. Mirrors
  the SPA gate so a stale browser tab cannot re-introduce the
  regression.
* `api/tasks/openapi/public_https.py::_resolve_operator_email` — drops
  the `noreply@elb-dashboard.local` fallback. The task now raises
  `ValueError` (caught at the top of `setup_openapi_public_https` and
  returned as `{status:"failed", step:"resolve_operator_email"}`) when
  neither caller nor `ELB_OPERATOR_EMAIL` env supplies a value.
* `api/services/k8s/ingress.py` — renames the patch helper to
  `patch_manifest_for_workload_pool` (and
  `fetch_install_manifest_for_workload_pool`) with `system_pool`-named
  backward-compat aliases. New constants
  `WORKLOAD_POOL_TOLERATION = {workload, Equal, blast, NoSchedule}`
  and `WORKLOAD_POOL_NODE_SELECTOR = {kubernetes.azure.com/mode: user}`.
  `SYSTEM_POOL_*` aliases preserved so callers / tests do not break.
* `api/tasks/openapi/auto_deploy.py` (new) — single source of truth for
  the auto-deploy policy (`AUTO_DEPLOY_ENV`, `auto_deploy_enabled`,
  `build_auto_openapi_payload`, `enqueue_openapi_deploy_after_aks_event`).
* `api/tasks/azure/lifecycle.py::start_aks` — now always tries to
  enqueue `deploy_openapi_service` (via the helper) unless the opt-out
  env is set; explicit `auto_openapi` payload still wins.
* `api/tasks/azure/provision.py::provision_aks` — same helper invoked
  immediately after `_publish(..., "completed", ...)`. Returns a new
  `openapi_task_id` field in the success payload.
* `web/src/components/SettingsPanel.tsx::PublicHttpsSection` — adds
  `isPublicLetsEncryptEmail` validator + auto-fill effect that calls
  `meApi.get()` and falls back to MSAL `account.username`. The Enable
  button is gated on `canEnable = canAct && emailValid`. The
  `Operator email` field label drops the `(optional)` suffix.

No Bicep / IaC changes.

## Validation evidence

* `uv run pytest -q api/tests` → **1562 passed in 30.37s**.
* Focused suites:
  * `api/tests/test_openapi_public_https.py` — 30 tests including the
    new `test_route_validate_operator_email_blocks_private_tlds` and
    the updated `test_operator_email_resolution` (now asserts
    `ValueError` on empty input).
  * `api/tests/test_azure_tasks.py` — adds
    `test_start_aks_auto_deploys_when_no_explicit_payload` and
    `test_start_aks_skips_openapi_when_opt_out_env_set`; updates
    `test_start_aks_enqueues_openapi_after_cluster_start` for the new
    `tenant_id` / `caller_oid` kwargs.
  * `api/tests/test_warmup_route.py` and
    `api/tests/test_openapi_deploy_contract.py` unaffected and still
    green.
* `uv run ruff check api` → All checks passed.
* `cd web && npm run -s build` → built in 4.41s (no errors).
* `cd web && npx vitest run` → **52 files / 389 tests passed**.

## Files touched

```
api/routes/aks/openapi.py
api/services/k8s/ingress.py
api/tasks/azure/lifecycle.py
api/tasks/azure/provision.py
api/tasks/openapi/__init__.py
api/tasks/openapi/auto_deploy.py        (new)
api/tasks/openapi/public_https.py
api/tests/test_azure_tasks.py
api/tests/test_openapi_public_https.py
web/src/components/SettingsPanel.tsx
```
