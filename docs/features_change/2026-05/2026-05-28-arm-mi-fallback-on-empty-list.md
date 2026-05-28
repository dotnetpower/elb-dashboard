---
title: Workspace auto-discovery falls back to backend MI proxy on empty ARM list
description: Collaborators with zero subscription-scope RBAC now see a populated dashboard because the SPA replays empty direct-ARM responses through the shared Managed Identity proxy.
tags:
  - user-guide
  - auth
---

# Workspace auto-discovery falls back to backend MI proxy on empty ARM list

## Motivation

A collaborator who signs in to the deployed SPA without holding `Reader`
at subscription scope landed on a fully-empty dashboard
(`Subscription ID: —`, every card showing the "Configure …" placeholder)
even though the backend's shared Managed Identity has full visibility
into the workload subscription. Asking each collaborator to acquire
subscription-scope `Reader` just to make the SPA render is over-broad
RBAC; the only place the user's token was required was the first ARM
metadata enumeration during auto-discovery.

The existing `useWorkspaceDiscovery` hook already tried to fall back
to the backend Managed Identity proxy (`/api/arm/subscriptions`,
`/api/arm/subscriptions/{sub}/resource-groups`) when the direct ARM
call **threw**. But Azure ARM returns an HTTP 200 with an empty array
for callers that have no subscription-scope role assignment, so the
fallback never fired — the SPA accepted "zero subscriptions" as the
truth and pushed the user into the SetupWizard.

## User-facing change

* A collaborator with **zero workload RBAC** now lands on a fully
  populated dashboard exactly like the deployer does. Subscription /
  resource-group / Storage / ACR / AKS cards all render, BLAST run and
  the API Reference page work, and the SetupWizard is no longer the
  default landing screen for an RBAC-less user.
* Deployers no longer need to grant `Reader` at subscription scope (or
  walk each collaborator through 4 hand-typed config values) just to
  unblock the dashboard. The MSAL bearer token continues to be required
  for authentication; the change only removes the *authorisation* gap
  on read-only ARM metadata enumeration.
* No change for users who already have direct ARM access — the direct
  call wins whenever it returns at least one subscription, so the
  backend round-trip is skipped on the common path.

## API / IaC diff summary

* New helper [web/src/lib/armWithMiFallback.ts](../../../web/src/lib/armWithMiFallback.ts)
  exposing `listWithMiFallback(direct, miProxy)`, which now treats an
  empty direct-ARM list the same way as a thrown error and replays the
  request through the backend MI proxy.
* [web/src/pages/Dashboard/useWorkspaceDiscovery.ts](../../../web/src/pages/Dashboard/useWorkspaceDiscovery.ts)
  routes both `auto-discover-subs` and `auto-discover-rgs` through the
  new helper. The per-subscription `try/catch` that previously swallowed
  RG enumeration errors silently is gone — `listWithMiFallback` returns
  an empty array on double-failure instead, so the discovery loop
  continues without losing other subscriptions.
* [web/src/components/SetupWizard/SetupWizard.tsx](../../../web/src/components/SetupWizard/SetupWizard.tsx)
  uses the same helper for its Step 1 subscription dropdown so a user
  who opens the wizard manually still gets the MI-proxy list.
* No backend, Bicep, or auth-layer changes. `/api/arm/*` continues to
  require `Depends(require_caller)`; the MI fallback only widens *which
  read-only ARM responses* the SPA is willing to consume.

## Validation evidence

* `cd web && npm test -- --run src/lib/armWithMiFallback.test.ts` —
  5/5 pass (covers non-empty direct, empty direct→MI, throw→MI,
  both-empty, and double-failure).
* `cd web && npm test -- --run` — 53 test files, 394 tests pass
  (no regressions in `configFromTags`, `aksManagedRg`, dashboard hooks).
* `cd web && npm run build` — production build clean.
