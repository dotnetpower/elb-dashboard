# quick-deploy: refuse to bake a wrong-tenant MSAL client into the cloud frontend

## Motivation

`scripts/dev/quick-deploy.sh` is the fast path another operator uses to push
images to the shared Container App (`all` and `frontend` targets). A fresh
clone's `.env` / `web/.env.local` frequently carries the *original*
developer's tenant/client MSAL values (`VITE_AZURE_CLIENT_ID` / `API_CLIENT_ID`).
When a different operator runs `quick-deploy.sh all` (or `frontend`) without
exporting the deploy target's MSAL overrides, the SPA is baked to authenticate
against App Registration **A** while the target's `api` sidecar only accepts
bearer tokens minted for App Registration **B**. The deploy reports success,
but every `/api/*` call from the browser returns **401** — the exact
"another operator deployed the wrong MSAL values" incident class.

The script already guards two siblings of this failure (a `localhost`
`VITE_API_BASE_URL` and a leaked `VITE_AUTH_DEV_BYPASS=true`). The
wrong-tenant client id was the remaining unguarded one.

## User-facing change

Before building the frontend image, `quick-deploy.sh` now compares the
`VITE_AZURE_CLIENT_ID` it is about to bake against the **target** Container
App's running `api` container `API_CLIENT_ID` env (the audience the api
validates bearer tokens against):

- **Mismatch** → the deploy aborts with a clear remediation message naming
  both client ids. Escape hatch for a deliberate App Registration rotation:
  `ELB_ALLOW_MSAL_CLIENT_MISMATCH=1`.
- **Target api has no `API_CLIENT_ID` yet** (first/bootstrap deploy) **or the
  `az containerapp show` query fails** (transient ARM error / read-only
  hiccup) → the check logs a note and continues, so a legitimate first
  rollout is never blocked.

The guard runs only when the frontend is actually built (`! --no-build`); the
`--no-build` GitHub Actions path preserves the already-baked env and is
unaffected. It mirrors the existing abort-with-escape-hatch style of the
auth-bypass guard rather than a default-OFF `STRICT_*` gate, because baking a
mismatched audience is always a bug — the safe default is to stop.

## API / IaC diff summary

- `scripts/dev/quick-deploy.sh`:
  - New helper `assert_msal_client_matches_target` (defined after
    `resolve_image_digest`).
  - Called in the `all` build path and the single `frontend` build path,
    immediately after the existing `localhost` / `VITE_AUTH_DEV_BYPASS`
    guards and before the version-stamp resolution.
- No backend, frontend, or Bicep changes. No new dependency.

## Validation evidence

- `bash -n scripts/dev/quick-deploy.sh` → syntax OK.
- JMESPath probe (`properties.template.containers[?name=='api'].env[] |
  [?name=='API_CLIENT_ID'].value | [0]`) verified to return the api's
  `API_CLIENT_ID` scalar when present and `None` (→ skip) when absent, using a
  representative Container App template fixture.
- Existing guards and the `--no-build` path are untouched.
