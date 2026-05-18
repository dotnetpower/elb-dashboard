# Frontend Runtime Environment Config

## Motivation

A quick frontend deployment built the Vite bundle without `VITE_AZURE_CLIENT_ID`, causing the deployed app to show `Setup Required` even though the API sidecar had the correct Entra App Registration settings.

## User-facing change

The SPA now reads `/runtime-config.js` before bootstrapping. The frontend sidecar generates that file at container startup from server environment variables, so a code-only frontend rebuild no longer has to bake every auth value into the static JavaScript bundle.

## API / IaC diff summary

- Added `web/entrypoint.sh`, which writes `/usr/share/nginx/html/runtime-config.js` from `VITE_*` env vars, falling back to `API_CLIENT_ID` and `AZURE_TENANT_ID` where appropriate.
- Added `web/public/runtime-config.js` as an empty local-development default.
- Updated auth/API config readers to prefer runtime config over build-time `import.meta.env`.
- Added frontend sidecar auth/runtime env vars to `infra/modules/containerAppControl.bicep`.
- Updated `scripts/dev/quick-deploy.sh` to load `.env`, `web/.env.local`, and `azd env`, pass frontend build args, set frontend Container App env vars, show ACR build logs, avoid blocking indefinitely on `azd env get-values`, and restore ACR public access immediately after builds.

## Validation evidence

- `bash -n scripts/dev/quick-deploy.sh web/entrypoint.sh` -> passed.
- `cd web && npm run build` -> passed.
- Touched-file ESLint -> passed.
- `az bicep build --file infra/modules/containerAppControl.bicep --stdout` -> passed.
- Deployed frontend image `acrelbnm5virmqrdi5c.azurecr.io/elb-frontend:runtime-env-fix-20260518` to Container App revision `ca-elb-control--0000052`.
- `curl /runtime-config.js` -> returned `window.__ELB_RUNTIME_CONFIG__` with `VITE_AZURE_CLIENT_ID` populated.
- `curl /api/health` -> `200`, revision `ca-elb-control--0000052`.
- Browser smoke -> sign-in screen rendered with `Sign in with Microsoft`; `Setup Required` no longer rendered.
- ACR network posture restored: `publicNetworkAccess=Disabled`, `defaultAction=Deny`.
