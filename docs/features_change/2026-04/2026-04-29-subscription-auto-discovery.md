# 2026-04-29 — Subscription auto-discovery in the SPA

## Motivation
Subscription ID was a free-text input — bad UX (user had to copy-paste a
GUID) and easy to typo. The browser already holds an MSAL-issued ARM
access token, so we can list the user's subscriptions directly.

## User-facing change
- Dashboard ConfigBar and Remote Terminal page now show a **dropdown** of
  every subscription the signed-in user can read. The first entry is
  auto-selected.
- "Sign in with Microsoft" now requests the API scope **and** ARM
  `user_impersonation` together, so the consent screen appears once.

## API/IaC diff summary
- `web/src/api/arm.ts` (new) — silent-token-then-popup ARM caller.
- `web/src/components/SubscriptionPicker.tsx` (new).
- `web/src/components/ConfigBar.tsx` + `pages/RemoteTerminal.tsx` use the
  picker in place of the text input.
- `web/src/pages/SignIn.tsx` requests both API and ARM scopes on login.
- `web/src/main.tsx` initialises MSAL fully (initialize → event callback →
  handleRedirectPromise) before rendering the React tree.
- `web/src/auth/msal.ts` switches cache to `localStorage` +
  `storeAuthStateInCookie` to survive the redirect leg.

## Validation evidence
- `npm run build` → success (1783 modules, no TS errors).
- Manual: signed in, dropdown populated with the tenant's subscriptions.

## Follow-ups
- Persist the chosen subscription per user in localStorage.
- Filter to subscriptions in the active tenant (currently lists all).
