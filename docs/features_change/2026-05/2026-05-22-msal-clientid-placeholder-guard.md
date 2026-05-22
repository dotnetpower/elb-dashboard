# MSAL clientId placeholder guard + local-run auto-pull

**Date:** 2026-05-22
**Scope:** `web/.env.example`, `web/src/config/runtime.ts`, `web/src/auth/msal.ts`, `web/src/App.tsx`, `scripts/dev/local-run.sh`

## Motivation

A user who cloned the repo locally and signed in with a different Entra account
hit `AADSTS700038: 00000000-0000-0000-0000-000000000000 is not a valid
application identifier`.

Root cause: `web/.env.example` shipped a placeholder all-zero UUID for
`VITE_AZURE_CLIENT_ID`. Anyone following the standard "copy `.env.example` to
`.env.local`" onboarding ended up baking that placeholder into the Vite build.
The existing `App.tsx` `CLIENT_ID_MISSING` guard (`!configValue(...)`) only
detected the **empty** case, so the placeholder UUID slipped past and reached
MSAL, which dutifully sent it to AAD.

The deployed pipeline was never affected — `scripts/dev/postprovision.sh`
already creates/reuses an Entra App Registration and injects the resolved
clientId via `--build-arg VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL` plus a
Bicep env var on the `frontend` sidecar.

## User-facing change

- Local clone-and-run no longer silently fails with AADSTS700038. If the
  clientId is empty **or** the all-zero placeholder **or** any non-UUID
  string, the SPA renders the existing "Setup Required" screen instead of
  initialising MSAL.
- `scripts/dev/local-run.sh web` auto-pulls `API_CLIENT_ID` from
  `azd env get-values` when `VITE_AZURE_CLIENT_ID` is unset or placeholder,
  so a developer who already ran `azd up` does not have to hand-edit
  `web/.env.local`.

## API / IaC diff summary

- `web/.env.example`: cleared `VITE_AZURE_CLIENT_ID` to empty with an
  explanatory comment.
- `web/src/config/runtime.ts`: added `isUsableClientId()` (rejects empty,
  all-zero UUID, and any non-UUID string) and `azureClientId()` accessor.
- `web/src/auth/msal.ts`: uses `azureClientId()`; warning message updated to
  point at the auto-pull path.
- `web/src/App.tsx`: `CLIENT_ID_MISSING` now uses `isUsableClientId()`.
- `scripts/dev/local-run.sh` (`web` case): when `VITE_AZURE_CLIENT_ID` is
  empty or the all-zero placeholder, query `azd env get-values | awk` for
  `API_CLIENT_ID` and export it for the `npm run dev` child process.
- `docs/joining-existing-deployment.md`: new dedicated page documenting
  `azd env refresh` as the supported way for a second teammate to bind a
  fresh clone to an existing deployment, plus the manual-clientId
  fallback, plus the "RBAC for the new teammate" sub-section that
  clarifies the deployed SPA needs no per-user workload RBAC and
  documents `grant-local-rbac.sh --user <upn>` for the deployer-grants
  case.
- `docs/troubleshooting.md`: new symptom-first index covering Setup
  Required / AADSTS700038, access_denied (deployed MI vs local user),
  network_blocked, missing workspace tag, AADSTS50011 redirect URI
  mismatch, stale `web/.env.local`, and `azd env refresh` without a
  selected env.
- `docs/get-started.md`: the previous inline "Join An Existing
  Deployment" section was removed (it sat awkwardly after Cost And
  Cleanup) and replaced with a short pointer to the two new pages.
- `docs/deployment-reference.md`: existing
  `access_denied` / `network_blocked` recovery snippet now also documents
  the `--user` flag.
- `mkdocs.yml`: new top-level nav entries for Joining An Existing
  Deployment and Troubleshooting, slotted between Get Started and
  Deployment Reference.
- `README.md`: short pointer in "Local development" linking to the new
  pages.
- No infra / Bicep / Container App changes.

## Validation

- `cd web && npm run build` → succeeds (`built in 7.71s`).
- `get_errors` on the three edited TS files → no errors.
- Backend tests unaffected; not re-run.
- Manual: copying `.env.example` → `.env.local` and running `npm run dev`
  with no other env now shows the "Setup Required" screen instead of the
  AADSTS700038 popup.
