# Control plane domain — MSAL redirect URI guidance + FQDN overlap fix

## Motivation

After binding the custom domain `https://dashboard.elasticblast.com` to the
dashboard Container App, interactive sign-in failed with `AADSTS50011` (redirect
URI mismatch): binding the hostname + managed certificate is not enough — the
same origin must also be registered as a **SPA redirect URI** on the MSAL app
registration. The Settings → Control plane domain section did not mention this
required step, and its "Fallback (FQDN)" row rendered the long
`*.azurecontainerapps.io` URL in a non-shrinking flex control, overlapping the
label.

## User-facing change

Settings → **Control plane domain**:

- Added an explicit **"MSAL redirect URI (required for login)"** block, shown
  whenever a custom domain is configured. It explains the `AADSTS50011` failure
  mode and renders a ready-to-run `az` command (pre-filled with the bound domain
  and the SPA app client id) with a **Copy** button. The command reads the
  current `spa.redirectUris`, appends the domain only when missing, and PATCHes
  via Microsoft Graph — so it is safe to re-run. It requires the Application
  Administrator (or Owner) role on the app registration; the dashboard backend
  intentionally does not hold Graph `Application.ReadWrite`, so this is operator
  copy-paste rather than a backend action.
- Extended the section intro to state that the bound domain must also be a SPA
  redirect URI on the app registration.
- Fixed the **Fallback (FQDN)** text overlap by replacing the fixed
  (`flexShrink: 0`) `Row` control with a stacked label-above-URL layout that
  wraps long URLs (`wordBreak: break-all`).

## API / IaC diff summary

None. Frontend-only, additive UI. No backend route, schema, or Bicep change.
Reuses existing `useClipboardFeedback` hook and `azureClientId()` runtime helper.

## Validation evidence

- `cd web && npm run build` → built clean (only the pre-existing chunk-size
  warning).
- `npx eslint src/components/settings/sections/ControlPlaneDomainSection.tsx`
  → exit 0.
- The live redirect URI for `https://dashboard.elasticblast.com` was added to
  app registration `14cf2a04-9985-4372-aa68-8d54c9adb75a` via Graph PATCH;
  `az ad app show --query spa.redirectUris` confirms the origin is present.
