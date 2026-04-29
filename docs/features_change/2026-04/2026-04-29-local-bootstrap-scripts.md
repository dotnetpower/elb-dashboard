# 2026-04-29 — Local bootstrap scripts (App Reg + Key Vault + secret)

## Motivation
A new contributor needed >5 manual Azure portal/CLI steps before the
control plane would run end-to-end. We can derive everything from
`az login`.

## User-facing change
- `scripts/dev/setup-app-registration.sh` — creates (or reuses) the App
  Registration, exposes `user_impersonation`, registers SPA redirect URI
  `http://localhost:8090`, requests ARM `user_impersonation`, and writes
  `web/.env.local` + a template `api/local.settings.json`.
- `scripts/dev/setup-keyvault.sh` — creates a Key Vault with RBAC mode +
  purge protection, grants the caller `Key Vault Secrets Officer`, and
  writes `KEY_VAULT_URI` into `api/local.settings.json`.
- `scripts/dev/generate-client-secret.sh` — appends a client secret on
  the App Registration and writes it as `API_CLIENT_SECRET` so OBO works.
- `scripts/dev/bootstrap-local.sh` — runs all three.

After `az login`, a single `./scripts/dev/bootstrap-local.sh` makes
`func start` + `npm run dev` immediately functional.

## API/IaC diff summary
- New scripts under `scripts/dev/` (executable).
- No code changes.

## Validation evidence
- Manual: setup-app-registration.sh on a fresh tenant created appId
  `f45292dc-…`, wrote env files, login succeeded with consent.

## Follow-ups
- Equivalent teardown script.
- Delete-and-recreate flag for App Registration (clean slate).
