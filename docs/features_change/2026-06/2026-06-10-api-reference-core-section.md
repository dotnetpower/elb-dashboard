---
title: API Reference "Core" control-plane section
description: A distinct, always-on Core section on the API Reference page that documents and lets you execute the dashboard-hosted ensure-running endpoint, separate from the AKS-hosted elb-openapi host.
tags:
  - ui
  - blast
---

# API Reference "Core" control-plane section

## Motivation

The API Reference page (`/api`) is built entirely from the live
[OpenAPI](https://www.openapis.org/) document served by `elb-openapi`, which runs
**inside** the AKS cluster. When the cluster is stopped, that whole page goes
blank — including the one endpoint a caller needs in exactly that situation:
`POST /api/aks/openapi/ensure-running`, which wakes the cluster.

That endpoint lives on the dashboard's own always-on `api` sidecar — a **different
host** from `elb-openapi` — so it must be documented and executable even while the
cluster is down.

## User-facing change

A new **Core** section now renders at the top of the API Reference page whenever a
cluster is selected, **including while the cluster is stopped**:

- **Distinct teal accent** and a "Control plane" badge so it reads as a different
  surface from the (blue-accented) spec-derived groups below it.
- A **host banner** that states plainly that these endpoints are served by the
  dashboard api sidecar at the current origin — *not* by the in-cluster
  `elb-openapi` service — and that this is why they answer when the cluster is
  stopped.
- A fully executable **"Try it"** card for `POST /api/aks/openapi/ensure-running`,
  exactly like the existing `/healthz`-style Try-it. The request body is pre-seeded
  with the resolved subscription / resource group / cluster, so "Send Request" is
  effectively one click. An "Observe phase only" example (`start: false`) is also
  provided. "Copy curl" emits a same-origin command with an MSAL bearer.

The endpoint reports the polled status vocabulary documented in the
[ensure-running change note](2026-06-10-aks-openapi-ensure-running.md):
`stopped` → `starting` → `warming` → `ready`.

## API / IaC diff summary

- `web/src/hooks/useOpenApiExecutor.ts` — new `dashboardApi` execution mode:
  requests go same-origin through `fetchApiRawNoRedirect` (MSAL bearer), and
  `buildCurl` emits an `origin + /api/...` command instead of the elb-openapi proxy
  path. Backward compatible (new param optional).
- `web/src/pages/apiReference/EndpointCard.tsx` — threads the optional
  `dashboardApi` flag to the executor.
- `web/src/pages/apiReference/coreEndpoints.ts` (new) — static, context-seeded
  definition of the Core endpoints (currently ensure-running).
- `web/src/pages/apiReference/CoreApiSection.tsx` (new) — the distinct section +
  host banner.
- `web/src/pages/ApiReference.tsx` — renders `CoreApiSection` for any selected
  cluster, regardless of power state.
- No backend / IaC change (the ensure-running route already exists).

## Validation evidence

- `npx vitest run` (web) → 781 passed, 85 files (includes the new
  `coreEndpoints.test.ts` and the new dashboardApi `buildCurl` test).
- `npm run build` (web) → type-check + production build clean.
- `npx eslint` clean on all touched files.
