# Local Dev Auth Bypass

## Motivation

Local development is configured with `VITE_AUTH_DEV_BYPASS=true`, but the dashboard and setup wizard still attempted direct browser MSAL calls to Azure Resource Manager before falling back to the local backend proxy. That made localhost show a stale session refresh banner even though local API discovery worked.

## User-facing change

When local auth bypass is enabled, localhost discovery now uses the Function API proxy first and does not trigger Microsoft Entra sign-in or a session refresh banner for subscription/resource-group discovery.

## API/IaC diff summary

- `web/src/pages/Dashboard.tsx` skips direct ARM subscription/resource-group calls when `VITE_AUTH_DEV_BYPASS=true`.
- `web/src/components/SetupWizard.tsx` skips direct ARM subscription calls when `VITE_AUTH_DEV_BYPASS=true`.
- No backend or infrastructure changes.

## Validation evidence

- Verified `http://localhost:8090/src/main.tsx` is serving `VITE_AUTH_DEV_BYPASS=true` from Vite.
- Verified local Function API `GET http://localhost:7071/api/arm/subscriptions` returns the active development subscription.
- Browser smoke test: `http://localhost:8090/` loads the setup wizard with the development subscription selected after the local API starts.