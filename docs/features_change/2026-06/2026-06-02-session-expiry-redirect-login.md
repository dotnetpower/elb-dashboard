---
title: Route expired sign-in sessions to the login page
description: Add an App-level session-expiry gate so a silently-expired MSAL session sends the user to the in-app sign-in page instead of leaving the dashboard mounted behind a stale banner, and scrub the last live az-jungha dev-profile mentions from the deploy script and auth-flow doc.
tags:
  - ui
  - auth
---

# Sign-in: route expired sessions to the login page; finish az-jungha cleanup

## Motivation
MSAL's `<AuthenticatedTemplate>` only checks that an account is still cached, not
that its tokens are still valid. When a session silently expired (refresh
failed, interaction required, or the API/ARM returned 401) the dashboard stayed
mounted behind a yellow "Sign in again" banner — the user had to notice the
banner and click it. A page reload, by contrast, correctly showed the sign-in
page. The request: when the sign-in session is broken, take the user to the
login page automatically.

Separately, the personal `az-jungha` az-profile alias still leaked into two live
surfaces (the deploy script's "not signed in" hint and the `auth-flow.md` doc).

## User-facing change
- When the browser sign-in session expires/breaks, the app now renders the
  in-app sign-in page (with a "Your sign-in session expired. Sign in again to
  continue." notice) in place of the dashboard, instead of only showing a
  banner. Signing in again re-opens the dashboard automatically once a fresh
  token is acquired.
- The expired sign-in page uses `prompt: "login"` to force a fresh credential
  prompt; the first-visit page keeps `prompt: "select_account"`.
- Deploy-script and documentation no longer name the personal `az-jungha`
  profile.

## Implementation summary
- `web/src/auth/sessionEvents.ts`: added a module-level session-issue store
  (`getAuthSessionIssue`, `clearAuthSessionIssue`, `useAuthSessionIssue` via
  `useSyncExternalStore`) alongside the existing CustomEvent bus. `notify*` now
  records the current issue; recovery clears it.
- `web/src/App.tsx`: new `AuthenticatedApp` gate renders `<SignIn expired … />`
  when a session issue is active, otherwise `<AppRoutes />`.
- `web/src/pages/SignIn.tsx`: optional `expired` / `expiredMessage` props add the
  expiry notice and switch the login prompt.
- `web/src/main.tsx`: the existing MSAL `LOGIN_SUCCESS` / `ACQUIRE_TOKEN_SUCCESS`
  callback now calls `clearAuthSessionIssue()`.
- `web/src/api/client.ts`: a successful silent token refresh clears the issue.
- `scripts/dev/quick-deploy.sh`, `docs/copilot/auth-flow.md`: dropped the
  `az-jungha` mentions.

## Validation evidence
- `cd web && npm test -- --run` → 62 files, 479 tests passed (includes the new
  `src/auth/sessionEvents.test.ts`, 5 tests).
- `cd web && npm run build` → clean production build.
- `npx eslint` on all touched `.ts/.tsx` → no findings.
- `grep -r az-jungha web/ scripts/ docs/copilot/ deploy.sh azure.yaml` → no live
  matches (only historical change notes remain, by design).
