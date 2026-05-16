# Env Example Scrub

## Motivation

Committed example environment files must not expose real Azure subscription,
tenant, or app registration identifiers. Vite also bakes `VITE_*` values into
the frontend bundle, so production frontend builds need an explicit injection
path that does not rely on committed real values.

## User-Facing Change

- `.env.example` and `web/.env.production` now contain placeholders only.
- Local developers keep real values in ignored `.env` / `web/.env.local` files.
- Production frontend image builds receive the tenant and API client id from
  `azd` / `postprovision.sh` build arguments.

## API/IaC Diff Summary

- No API route changes.
- No Bicep resource changes.
- `web/Dockerfile` accepts Vite build args and exports them for `npm run build`.
- `scripts/dev/postprovision.sh` passes frontend MSAL build args from `azd` env.

## Validation Evidence

- `grep` scan confirmed the tracked env files no longer contain the previous
  real tenant, subscription, or app registration ids.
- `get_errors` reported no errors for the edited env, Dockerfile, shell, and
  change-note files.
