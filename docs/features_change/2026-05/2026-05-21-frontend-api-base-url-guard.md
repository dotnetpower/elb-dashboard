# Frontend API base URL guard + cloud env recovery

**Date**: 2026-05-21
**Scope**: `scripts/dev/quick-deploy.sh`, deployed Container App `ca-elb-control` (frontend env)

## Motivation

The cloud dashboard at
`https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io`
showed every monitoring card as **Network error** and the subscription
selector as **Error**. Root cause: the `frontend` container in revision
`--0000110` had

```
VITE_API_BASE_URL = http://localhost:8085
```

baked into its environment. `web/entrypoint.sh` writes that value into
`/runtime-config.js`, so the browser-side SPA was issuing every `/api/*`
call against the **operator's own laptop** (`http://localhost:8085`), not
the Container App. That also explained the stale dropdown values
(`rg-elb-01`, `elbacr01 · rg-elbacr-01`, `elbstg01 · rg-elb-01`) — those
came from the operator's local dev API working against a different
environment. `/api/me` returned 401 because the MSAL token issued for the
cloud client id was rejected by the local API.

How the poisoned value got there: `scripts/dev/local-run.sh web`
exports `VITE_API_BASE_URL=${VITE_API_BASE_URL:-http://localhost:8085}`
in the calling shell. Running `scripts/dev/quick-deploy.sh frontend` in
the same shell carried that export into the build args + the
`az containerapp update --set-env-vars` patch.

## User-facing change

* Cloud dashboard cards (AKS, ACR, Storage, Terminal, Subscription/RG
  selectors) now reach the same-origin backend again. No code change to
  the SPA — fixing the container env was sufficient because
  `runtime-config.js` is generated at container start.
* `quick-deploy.sh frontend` now refuses to run if
  `VITE_API_BASE_URL` points at `localhost`, `127.*`, `0.0.0.0`, or
  `[::1]`, with the message:

  > VITE_API_BASE_URL='http://localhost:8085' points at the local host —
  > refusing to bake that into the cloud frontend. Run
  > 'unset VITE_API_BASE_URL' (or export VITE_API_BASE_URL='') and retry.
* `quick-deploy.sh` no longer inherits `VITE_API_BASE_URL` from
  `web/.env.local`. That file is the local-dev convention for the Vite
  dev server (`vite dev` + `local-run.sh web`) and pins the value to
  `http://localhost:8085`. The loader for `web/.env.local` now accepts
  a skip-list, and `quick-deploy.sh` passes `VITE_API_BASE_URL` to it.
* `web/nginx.conf` now serves `/runtime-config.js` with
  `Cache-Control: no-store, must-revalidate`. Previously the file went
  out with no `Cache-Control`, so browsers applied heuristic disk
  caching and kept the poisoned config even after the cloud env had
  been fixed.

## API / IaC diff

* No API surface change.
* No Bicep change.
* `scripts/dev/quick-deploy.sh`:
  * Regex guard in the `SIDECAR == "frontend"` branch rejects loopback
    values for `VITE_API_BASE_URL`.
  * `load_simple_env_file` now accepts a skip-list of variable names;
    `web/.env.local` is loaded with `VITE_API_BASE_URL` in the skip-list
    so the cloud build never inherits the dev-loopback value.
* `web/nginx.conf`: dedicated `location = /runtime-config.js` block adds
  `Cache-Control: no-store, must-revalidate` and `expires off`.
* Cloud env patched out-of-band (already shipped earlier in the same
  session, before the image rebuild):

  ```sh
  az containerapp update -n ca-elb-control -g rg-elb-ca \
    --container-name frontend --set-env-vars VITE_API_BASE_URL=
  ```
* Rebuilt frontend image `acrelbnm5virmqrdi5c.azurecr.io/elb-frontend:20260521231605`
  rolled out as `ca-elb-control--0000112`.

## Validation

1. **runtime-config.js (before)**:
   `{"VITE_API_BASE_URL":"http://localhost:8085", ...}` — broken.
2. **`az containerapp update` issued at 14:01 UTC**, revision
   `ca-elb-control--0000111` reported `latestReadyRevisionName` within
   ~12 s.
3. **runtime-config.js (after env patch, revision --0000111)**:
   `{"VITE_API_BASE_URL":"","VITE_AUTH_DEV_BYPASS":"false",...}` — same-origin restored.
4. **Guard smoke test**:
   ```
   VITE_API_BASE_URL=http://localhost:8085 ... → REJECTED: http://localhost:8085, exit 11
   ```
5. **`bash -n scripts/dev/quick-deploy.sh`**: syntax OK.
6. **`load_simple_env_file` skip-list smoke test**: after the cleanup,
   running `quick-deploy.sh frontend` with `VITE_API_BASE_URL` unset in
   the shell no longer reintroduces `http://localhost:8085` from
   `web/.env.local` — the guard does not trip, build proceeds.
7. **Frontend image rebuild + rollout**: tag `20260521231605`, revision
   `ca-elb-control--0000112` became `latestReadyRevisionName` within
   ~10 s.
8. **runtime-config.js (after rebuild, revision --0000112)**:
   ```
   HTTP/2 200
   content-type: application/javascript
   cache-control: no-store, must-revalidate
   ```
   Body still reports `VITE_API_BASE_URL=""` (same-origin).
9. **/index.html headers** unchanged: `cache-control: no-cache`
   (matches the existing `location = /index.html` block).

## Follow-up

* Existing user tabs that still hold the cached `runtime-config.js`
  body need a hard reload once. From the next deploy onwards, the
  `no-store` header prevents this class of staleness.
* If a similar poisoned env slipped into the `api`/`worker`/`beat`
  containers, future deploys would also propagate it. Those containers
  do not read `VITE_*`, so the immediate blast radius is limited to the
  SPA.
