# API Reference page — prefetch on Dashboard + refresh guard during deploy

**Date:** 2026-05-16

## Motivation

Two friction points reported on the API Reference (`/docs`) page:

1. **"Discovering OpenAPI service on AKS..." spinner is slow.** The page chains four queries (AKS list → ACR tags → Service IP via k8s API → openapi.json proxy). Steps 3 and 4 hit the cluster and routinely take 2–5 s end-to-end. Until now, this work only started when the user navigated to `/docs`.
2. **Refreshing during a deploy loses progress.** The deploy task itself keeps running (the Celery task is server-side and the instance id is persisted to localStorage), but the user no longer sees the live progress timer and frequently re-clicks Deploy thinking it failed.

## User-facing change

* **Pre-warm.** While the user is on the Dashboard with a complete `savedConfig` (sub + workload RG + acrRG + acrName), the same four queries the API Reference page uses are silently fired in the background, with the same query keys and `staleTime` values. When the user navigates to `/docs`, the data is already in the React Query cache and the spinner usually doesn't show at all.
* **Refresh guard.** While an OpenAPI deploy is in-flight (`startingDeploy || deployInProgress`), the page registers a `beforeunload` handler that triggers the browser's native "Leave site?" confirmation. The handler is removed as soon as the deploy ends or the panel unmounts.

## API / IaC diff summary

Backend, Bicep, terminal sidecar — all unchanged. Pure SPA change.

* **New file:** [web/src/hooks/usePrefetchApiReference.ts](../../../web/src/hooks/usePrefetchApiReference.ts)
  * Calls `useQueryClient().prefetchQuery` for `["aks", sub, rg]`, `["acr", sub, acrRg, acrName]`, then chains `["openapi-svc", sub, rg, clusterName]` and `["openapi-spec", sub, rg, clusterName]` once the cluster name resolves.
  * Skips the `serviceIp` / `proxyOpenApiSpec` legs when the cluster is not Running, to avoid pre-failing into the cluster-stopped UX.
  * Defers 250 ms after mount so it never competes with the dashboard's initial paint.
  * Swallows errors silently — the page surfaces its own UX if a query fails on actual navigation.
* **Edit:** [web/src/pages/Dashboard.tsx](../../../web/src/pages/Dashboard.tsx) — imports and invokes `usePrefetchApiReference` with the active `config`.
* **Edit:** [web/src/components/OpenApiDeployPanel.tsx](../../../web/src/components/OpenApiDeployPanel.tsx) — new `useEffect` that adds/removes a `beforeunload` listener while the deploy is mid-flight (`startingDeploy || deployInProgress`).

## Notes & non-goals

* **Same query keys are critical.** TanStack Query identifies cache entries by serialised key. The hook reuses the exact tuples that `ApiReference.tsx` registers (`["aks", sub, rg]`, etc.), so on navigation the page hits the cache instead of issuing a new fetch.
* **Different `staleTime` on the dashboard's own `["gs-aks", ...]` queries** — the dashboard's monitoring cards use a separate `gs-` prefix with shorter `staleTime: 60_000` because they refresh more aggressively. We deliberately leave those alone; the prefetch only adds the `["aks", sub, rg]` (300 s stale) tuple that `/docs` consumes.
* **`beforeunload` cannot block forced reloads** (Cmd-Shift-R, devtools "Empty Cache and Hard Reload"). The deploy task is already idempotent server-side — the prompt is a UX guard, not a correctness one. The localStorage-backed instance id continues to let the page resume tracking after a hard reload.
* **Image build is not guarded.** The user explicitly asked about "openapi 이미지 배포" (the OpenAPI deploy panel), not the ACR build flow. The latter is shorter-lived and already idempotent on retry.

## Validation evidence

```
$ cd web && npm run build
✓ built in 6.40s

$ cd web && npx tsc --noEmit
(no output — strict TS clean)
```

Manual verification path (local compose):

1. Open the Dashboard at `http://127.0.0.1:18080/` with a workspace already configured.
2. Open DevTools → Network. Within ~250 ms after the dashboard renders, observe the four prefetch requests (`/api/monitor/aks`, `/api/monitor/acr`, `/api/monitor/aks/service-ip`, `/api/aks/openapi/spec`).
3. Click **API** in the side nav. The page renders the spec without the "Discovering OpenAPI service on AKS..." card.
4. From the API page, hit **Update** on the OpenApiDeployPanel; while progress is "Deploying OpenAPI service", press F5 / Ctrl-W. The browser shows its native confirmation dialog.
5. Cancel the dialog. Wait for the deploy to finish; refresh now proceeds without prompting.
