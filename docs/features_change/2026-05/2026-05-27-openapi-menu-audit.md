# OpenAPI menu audit — wire `acr_resource_group`, drop stale fallbacks, sync mocks

## Motivation

Deep review of the dashboard's OpenAPI menu surface (`/api/` page +
`OpenApiDeployPanel` + `/api/aks/openapi/*` routes + `deploy_openapi_service`
task) surfaced several gaps where the SPA's saved config and the deployed
`elb-openapi` pod's runtime env were silently disconnected, and pinned
metadata had drifted from the actual `IMAGE_TAGS` source of truth.

## User-facing change

1. **`elb-openapi` pod now points at the user's real ACR resource group.**
   Previously the route discarded `acr_resource_group` from the SPA
   payload, the Celery task defaulted to a hardcoded `rg-elbacr-01`, and
   the manifest set `ELB_ACR_RESOURCE_GROUP` to that wrong value. Any
   tenant whose ACR lives in a differently-named RG would observe
   `elb-openapi` reading the wrong ACR when resolving image lookups /
   credentials. Now the panel forwards `acrResourceGroup` end-to-end
   (SPA `ApiReference.tsx` → `OpenApiDeployPanel` → `useDeployTask` →
   `aksApi.deployOpenApi` → `/api/aks/openapi/deploy` route → Celery
   task → manifest env).
2. **Dead `image_tag = "4.9"` fallback removed.** The deploy task now
   reads `IMAGE_TAGS["elb-openapi"]` directly. The previous fallback
   would have silently regressed to an old tag if the key were ever
   removed from `IMAGE_TAGS`.
3. **`image_tags.py` documents the `4.x` vs upstream `3.6.0` tag scheme.**
   Future maintainers no longer have to read commit messages to learn
   that dashboard tag `4.14` ↔ sibling repo `docker-openapi/app/main.py`
   `VERSION = "3.6.0"`.
4. **Docs-preview mock OpenAPI image tag synced to `4.14`.** Previously
   the mock pinned `2026.05.21` (a date-format tag inherited from the
   sibling Container Apps images). The OpenAPI menu's pinned-vs-deployed
   comparison rendered wrong on the docs preview site.

## API / IaC diff summary

- `POST /api/aks/openapi/deploy` body now accepts an optional
  `acr_resource_group` field. Backward compatible: legacy callers that
  omit it still get the hardcoded `rg-elbacr-01` fallback inside the task.
- `aksApi.deployOpenApi` gains a 7th optional positional argument
  `acrResourceGroup`. Existing call sites without the argument keep
  compiling (TypeScript optional positional).
- `OpenApiDeployPanel` adds a required `acrResourceGroup: string` prop.
  Sole call sites are in `ApiReference.tsx`; both updated.
- No Bicep / infra changes.

## Validation

- `uv run pytest -q api/tests/test_openapi_deploy_contract.py
  api/tests/test_openapi_task.py api/tests/test_openapi_deployment.py
  api/tests/test_openapi_public_https.py
  api/tests/test_openapi_tls_hook.py
  api/tests/test_openapi_proxy_route.py` — 56 passed.
- `uv run pytest -q api/tests` — 1491 passed.
- `cd web && npm test -- --run` — 376 passed (51 files).
- `cd web && npx tsc --noEmit` — clean.
- `cd web && npm run build` — built in 8.06 s.
- `uv run ruff check api/routes/aks/openapi.py api/tasks/openapi/deploy.py
  api/services/image_tags.py api/tests/test_openapi_deploy_contract.py` —
  All checks passed.
- New regression test
  `test_openapi_deploy_route_forwards_acr_resource_group` locks the
  route → task contract for `acr_resource_group`.

## Follow-up (P2/P3) — 2026-05-27 second pass

Three P2/P3 findings from the original audit were left open; this
follow-up closes them.

### 1. Peering recovery hint on deploy status

`GET /api/aks/openapi/deploy/{id}/status` now injects the same
additive `recovery_action: peer_with_platform` / `recovery_hint` pair
already returned by the proxy / spec routes whenever the task's
failure looks like an upstream-reach problem. Classifier
(`_deploy_failure_is_upstream_reach`):

- `openapi_deploy.status == "no_ready_replica"` with empty
  `external_ip` (LB never came up — the canonical VNet-peering
  symptom on AKS-auto VNets), OR
- diagnostic events mention `"no endpoints available"`, OR
- the error string contains `unreachable` / `timed out` / `no route
  to host` / `i/o timeout` / `connection refused`.

The keys are added at envelope root only — `runtime_status`,
`custom_status`, `output` and other fields are unchanged so legacy
SPA builds keep working. Image-pull / scheduling / Workload-Identity
failures intentionally do **not** receive the hint (a Repair-Peering
button would mislead the operator).

### 2. Real cancel route for OpenAPI deploy

New `POST /api/aks/openapi/deploy/{task_id}/cancel` (in
[api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py))
mirrors the existing `POST /api/aks/cancel-provision/{task_id}`
contract exactly:

- ownership gate via the shared `_enforce_task_ownership` helper
  imported from `api.routes.aks.cancel`;
- idempotent — already-terminal tasks return 200 with
  `was_running: false` and `settle_after_seconds: 0`;
- revokes with `terminate=True, signal="SIGTERM"`;
- best-effort `JobStateRepository` lookup + `update_state(...,
  status="cancelled", error_code="cancelled_by_user")` when a state
  row exists (OpenAPI deploy currently does not persist one, so
  `job_id` is typically `null` — matches cancel-provision's orphan
  contract);
- `settle_after_seconds: 10` (vs 20 s for cancel-provision) because
  the OpenAPI probe loop yields every 5-10 s.

The SPA `DeployActions` Cancel button now calls
`aksApi.cancelOpenApiDeploy(taskId)` through `handleCancelTracking`
in `useDeployTask` instead of only clearing localStorage. Local
state is cleared first so the UI stays responsive even if the revoke
call hangs; the response error is surfaced in the existing
`deployError` channel. Tooltip updated to reflect the real revoke
behaviour ("worker honours SIGTERM at the next probe yield ~10 s").

### 3. Expanded proxy deny-list

`_OPENAPI_PROXY_DENIED_PATH_TOKENS` gained the missing dashed
siblings `"/debug-"`, `"/private-"`, `"/sudo-"` so every privileged
family now carries the same three-way coverage (`/x/` for segment,
`/x?` for query-stripped exact, `/x-` for dasherised sibling). A new
`test_openapi_proxy_denied_tokens_keep_symmetric_coverage` parametric
guard locks the symmetry so a future edit cannot silently drop a
variant. The existing single-token rejection tests still cover
`/admin/*` and `/internal/*`; three new parametrised cases exercise
`/v1/debug-info`, `/v1/private-keys`, `/v1/sudo-mode/promote`.

### Follow-up — API / IaC diff summary

- `POST /api/aks/openapi/deploy/{task_id}/cancel` is a new route. The
  response shape reuses `AksCancelProvisionResponse` so the SPA reuses
  its existing cancel-toast UX.
- `GET /api/aks/openapi/deploy/{id}/status` gains optional
  `recovery_action` and `recovery_hint` envelope-root fields. Additive,
  backward-compatible — they are absent on success, on still-running
  tasks, and on non-peering failures.
- `aksApi.cancelOpenApiDeploy(taskId: string)` added to
  [web/src/api/aks.ts](../../../web/src/api/aks.ts).
- `DeployActions.tsx` Cancel button tooltip rewritten; the button now
  drives a real Celery revoke via `useDeployTask.handleCancelTracking`.
- No Bicep / infra changes.

### Follow-up — Validation (2026-05-27)

- `uv run pytest -q api/tests/test_openapi_deploy_status_and_cancel.py` —
  14 passed (6 status-envelope cases + 4 cancel-route cases + 4
  deny-list cases).
- `uv run pytest -q api/tests` — **1505 passed** (was 1491; +14 from
  the new file; no regressions in `test_openapi_*`, `test_route_contracts`,
  `test_aks_cancel_provision`).
- `cd web && npm test -- --run` — **377 passed** across 51 files
  (was 376; +1 in `src/api/aks.test.ts` for `cancelOpenApiDeploy`).
- `cd web && npx tsc --noEmit` — clean.
- `cd web && npm run build` — built in 6.90 s.
- `uv run ruff check api` — All checks passed.
- Diff audit: `git status --short` shows only the expected files
  (`api/routes/aks/openapi.py`, `api/tests/test_openapi_deploy_status_and_cancel.py`,
  `api/tests/test_route_contracts.py`, `web/src/api/aks.ts`,
  `web/src/api/aks.test.ts`,
  `web/src/components/OpenApiDeployPanel/DeployActions.tsx`,
  `web/src/components/OpenApiDeployPanel/useDeployTask.ts`, this change
  note).
- Consumer search: `aksApi.cancelOpenApiDeploy` has one production
  caller (`useDeployTask.handleCancelTracking`) plus one test caller.
  `_OPENAPI_PROXY_DENIED_PATH_TOKENS` is only referenced from
  `_enforce_openapi_proxy_target_path` and the new symmetry guard
  test. `recovery_action`/`recovery_hint` envelope fields flow
  `aksApi.openApiDeployStatus` → `useDeployTask` (new
  `deployRecoveryAction` / `deployRecoveryHint` exports) →
  `OpenApiDeployPanel` → `DeployStatusBanner`, which now renders the
  shared `RepairPeeringButton` (the same component already wired for
  the spec / proxy / public-HTTPS endpoints in
  [ApiReference.tsx](../../../web/src/pages/ApiReference.tsx)) inline
  below the deploy error message. Existing `ApiReference.tsx` /
  `EndpointCard.tsx` usages of the button are unchanged.
