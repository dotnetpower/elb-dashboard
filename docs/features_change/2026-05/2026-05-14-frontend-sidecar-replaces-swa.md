# 2026-05-14 — Frontend sidecar replaces the Static Web App

## Motivation

The user asked whether the SPA could also be folded into the bundled Container
App as a sixth sidecar. Yes — it removes another billable Azure resource,
collapses the SPA and api into a single origin (no CORS surface, one MSAL
redirect URI), and keeps everything inside the same `ca-elb-control` revision.

## User-facing change

None at runtime, except the production hostname changes from
`<name>.azurestaticapps.net` to `<app>.<region>.azurecontainerapps.io` (or
whatever custom domain is mapped to the Container App ingress). Operators see
the SPA, the api, and the terminal at the same origin; the browser DevTools
no longer shows cross-origin requests for `/api/*`.

## Architecture diff summary

| Area | Previous (5 sidecars + SWA) | Now (6 sidecars, no SWA) |
|------|------------------------------|--------------------------|
| SPA hosting | Azure Static Web Apps (`Microsoft.Web/staticSites`) | `frontend` sidecar (nginx:alpine) inside `ca-elb-control` |
| Browser → SPA | SWA hostname (`*.azurestaticapps.net`) | Container App ingress hostname |
| Browser → API | SWA linked-backend rewrite to the Function App | Same origin: api sidecar matches `/api/*` directly, reverse-proxies everything else to the frontend at `127.0.0.1:8081` |
| CORS | SPA → Function App had a separate origin | None (same origin) |
| MSAL redirect URI | SWA hostname | Container App ingress hostname (one-time App Registration update at cutover) |
| TLS | SWA-managed cert + free CDN | Container Apps-managed cert; no CDN (escalation: put Front Door in front) |
| `staticwebapp.config.json` `routes`/`globalHeaders`/`navigationFallback` | SWA-interpreted JSON config | nginx.conf `location` blocks + `add_header` lines + `try_files` |
| SPA cache busting | SWA defaults | Image tag = SPA build hash; nginx serves `/assets/*` immutable, `/index.html` no-cache |
| Resource count | 1 Container App + 1 SWA | 1 Container App |
| Identities | `id-elb-control` (5 sidecars) | `id-elb-control` (6 sidecars; frontend has no Azure SDK and inherits the MI but cannot use it) |

## Files changed

- `docs/container-apps-migration.md`:
  - Decision Summary lists six sidecars and explicitly removes the SWA.
  - "Explicitly removed from the prior plan" gains a Static Web App row.
  - Resources to Create removes the SPA row and adds Static Web Apps to the
    "Not created" line.
  - Target Architecture diagram updated: removes the "Static Web App"
    intermediary, adds the `frontend` sidecar, shows the api as the single
    public ingress that reverse-proxies `/` to the frontend and exposes
    `/api/*` directly.
  - Component Plan adds a `frontend` row and a clearer `api` row (with the
    reverse proxy responsibility called out).
  - Service Boundaries: new `frontend` sidecar section (nginx config,
    security headers, image build, SWA → sidecar replacement table). The
    `api` sidecar section gains the catch-all reverse proxy responsibility.
  - Identity table updated: shared MI now covers six sidecars; explicit note
    that `frontend` is `nginx:alpine` and cannot use the MI.
  - Networking subnet description references six sidecars.
  - Phase 2 picks up the `elb-frontend` image build, the sixth sidecar in
    the Container App definition, and the api reverse proxy.
  - Phase 5 (cutover) gains explicit steps: update MSAL redirect URI, run
    SPA via the frontend sidecar in staging, switch production hostname,
    keep SWA + Function App for one release window, then delete the SWA
    resource.
  - Cutover Checklist gains three new rows: SPA served from same origin
    with no CORS preflight observed, MSAL redirect URI updated and sign-in
    works, SWA resource deleted (or marked for deletion).
  - Risks gains four new rows: loss of CDN, MSAL redirect URI mismatch,
    nginx misconfig, SPA cache busting.
  - Open Decisions gains a "SPA hosting" row and updates the "Topology"
    row to "six sidecars".
  - First Implementation Slice notes the existing SWA continues to serve
    the SPA until phase 2 lands.
- `README.md`: Architecture Planning bullet updated for six sidecars,
  `frontend` listed first, "no Static Web App" in the negation list.

## Code consequences (follow-up tickets)

These are not done in this PR (planning only):

1. Add `web/Dockerfile` (multi-stage: `node:20-alpine` builder running
   `npm ci && npm run build`, then `nginx:alpine` with `dist/` copied into
   `/usr/share/nginx/html` and a custom `nginx.conf`).
2. Move `web/staticwebapp.config.json` rules into the new `nginx.conf`:
   - `routes`/`navigationFallback` → `location / { try_files $uri /index.html; }`
   - `globalHeaders` → `add_header` lines (X-Content-Type-Options,
     X-Frame-Options, Referrer-Policy, Strict-Transport-Security,
     Content-Security-Policy)
   - Cache rules: `location /assets/ { add_header Cache-Control
     "public, immutable, max-age=31536000"; }` and `location = /index.html
     { add_header Cache-Control "no-cache"; }`
3. Add the `frontend` sidecar to the Container App definition with no
   ingress, listening on `127.0.0.1:8081`.
4. Add the api sidecar's catch-all reverse proxy (FastAPI route or, if it
   gets in the way of the WebSocket upgrade, a thin starlette/uvicorn
   middleware) that forwards every non-`/api/*` request to
   `http://127.0.0.1:8081` and streams the response back unchanged.
5. Update the MSAL App Registration: add the Container App ingress
   hostname as an additional redirect URI; remove the SWA hostname after
   cutover.
6. Update `azure.yaml` to drop the `web` service when the SPA moves into
   the api image's deploy pipeline (or build the `elb-frontend` image
   alongside the api image in the same azd hook).
7. Delete the `Microsoft.Web/staticSites` Bicep resource (or mark for
   deletion in the next cleanup window).
8. Add an integration test that runs the api + frontend + a tiny static
   asset suite, asserting:
   - `GET /` returns the SPA `index.html` with the right CSP header.
   - `GET /assets/<known-hash>.js` returns the asset with
     `Cache-Control: public, immutable, max-age=31536000`.
   - `GET /api/health` returns 200 from the same origin (no CORS
     preflight needed for the SPA in the browser).
   - `GET /some/deep/spa/route` returns the SPA `index.html` with 200
     (navigation fallback).
   - `GET /api/this-does-not-exist` returns 404 from the api, NOT the
     SPA fallback.

## Validation evidence

Documentation-only change. Verified there is no remaining "five sidecars"
or active SWA recommendation:

```bash
grep -nE "five sidecars|four sidecars" docs/container-apps-migration.md
# (no output)

grep -nE "Static Web App|staticSites|staticwebapp" docs/container-apps-migration.md | grep -v "removed\|deleted\|Old\|Not created\|migration scope\|first slice does not need"
# Returns only the "old → new" replacement table row in Service Boundaries.
```
