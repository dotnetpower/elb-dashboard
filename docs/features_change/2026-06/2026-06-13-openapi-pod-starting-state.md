---
title: API Reference shows "Starting…" while elb-openapi pod boots
description: The OpenAPI spec route now distinguishes a still-starting / not-ready elb-openapi pod from a genuine VNet-peering break, so the API Reference page renders a calm "Starting…" state with auto-refresh instead of a red "Repair VNet peering" error during an image cold-pull.
tags:
  - blast
  - ui
---

# API Reference shows "Starting…" while elb-openapi pod boots

## Motivation

When the `elb-openapi` pod is rescheduled onto a freshly scaled-up blastpool
node, the kubelet cold-pulls the ~370 MB image, which takes ~90 s. During that
window the internal LoadBalancer already has an IP, but the pod has no Ready
endpoint, so the dashboard's `GET /api/aks/openapi/spec` proxy fetch fails.

Previously the route attributed *every* such fetch failure to a missing VNet
peering and returned `degraded_reason: openapi_endpoint_unreachable` plus the
`recovery_action: peer_with_platform` hint. The API Reference page then rendered
a red "The elb-openapi service did not respond — Repair VNet peering" error.
That reads as a failure for a benign, self-resolving startup state — the symptom
that prompted this change (a pod that was merely `ContainerCreating` looked
broken).

## User-facing change

* While the `elb-openapi` pod is still starting, the API Reference page now
  shows a calm **"elb-openapi is starting"** panel (accent spinner, "~2 minutes
  on a fresh node while the image is pulled") instead of the red peering error.
  The page **auto-polls every 8 s** and flips to the live API Reference on its
  own once the pod serves — no manual refresh.
* If the pod is up but failing readiness (e.g. `CrashLoopBackOff`,
  `ImagePullBackOff`), the page shows a muted **"elb-openapi pod is not ready"**
  warning that points at the pod logs — still **not** the peering-repair
  affordance, because a crash-looping pod is not a peering problem.
* A genuinely Ready-but-unreachable pod (the real VNet-peering case) keeps the
  existing red "Repair VNet peering" error unchanged.

## API / IaC diff summary

* New service module `api/services/openapi/pod_phase.py`:
  * `classify_openapi_pod_state(pods, *, ready_replicas, desired_replicas)` —
    pure classifier returning `ready` / `starting` / `failed` / `absent` /
    `unknown` from Deployment ready-replica count + container waiting reasons.
  * `get_openapi_pod_startup_state(...)` — read-only probe (Deployment
    ready-replicas + `app=elb-openapi` pod list). Never raises; degrades to
    `unknown` on any Kubernetes read error.
* `api/routes/aks/openapi.py` `aks_openapi_spec`: when the spec fetch fails, the
  route now probes the pod startup state. `starting` → 200 placeholder with
  `degraded_reason: openapi_pod_starting`; `failed` → `openapi_pod_not_ready`.
  Both omit the peering `recovery_action`. `ready` / `unknown` fall through to
  the existing `openapi_endpoint_unreachable` + peering hint, so the genuine
  peering case is unchanged.
* `web/src/pages/apiReference/openApiPodStartup.ts`: `readOpenApiPodStartup`
  discriminator + `OpenApiSpecDegraded` type.
* `web/src/pages/ApiReference.tsx`: new `OpenApiPodStartingState` panel,
  rendered for the two pod-startup degraded reasons; `specQuery` gains a
  `refetchInterval` that polls every 8 s while `openapi_pod_starting` (and stops
  for `openapi_pod_not_ready` to avoid hammering a known-bad rollout).

No infra/Bicep changes. No new Azure permissions — the probe reuses the existing
shared MI Kubernetes read path (`k8s_get_deployment_ready_replicas` + a label-scoped
pod list).

## Validation evidence

* `uv run pytest -q api/tests/test_openapi_pod_phase.py` — 13 passed (classifier
  matrix + 4 route-wiring cases: starting / not-ready / ready-keeps-peering /
  unknown-keeps-peering).
* `uv run pytest -q api/tests/test_openapi_proxy_route.py api/tests/test_openapi_deployment.py api/tests/test_openapi_tls_hook.py api/tests/test_route_contracts.py`
  — 57 passed (no regression in the existing spec/proxy degraded-payload tests).
* `cd web && npx vitest run src/pages/apiReference/` — 34 passed (incl. new
  `openApiPodStartup.test.ts`, 5 cases).
* `cd web && npm run build` — clean; `npx eslint` on the changed files — clean.
* `uv run ruff check api` — clean.

> Note: the full `uv run pytest -q api/tests` run also surfaced two failures
> (`test_facade_contract_covers_all_string_target_monkeypatches` referencing
> `api.tasks.azure.enable_aks_container_insights`, and
> `test_bicep_references_every_guard_key`) that belong to unrelated in-progress
> work in the same working tree (container-insights / Bicep guard keys), not to
> this change.
