# Auth Session Refresh Banner

## Motivation

A browser session can become stale while the Static Web App still has cached UI state. In that case users may see loading or request errors even though the backend is healthy, and signing in again resolves the issue.

## User-facing change

When API or ARM token refresh fails, or an authenticated API request returns HTTP 401, the app now shows a top-of-page session banner with a `Sign in again` action. The action starts a fresh Microsoft Entra login flow with `prompt=login`.

## API/IaC diff summary

- Added a small auth session event helper in `web/src/auth/sessionEvents.ts`.
- API and ARM clients now publish a session issue event when token state looks stale.
- The main layout now renders a persistent session refresh banner for authenticated pages.
- No backend or IaC changes.

## Validation evidence

- `npm run build` passed.
- `azd deploy web --no-prompt` deployed the production Static Web App.
- Browser smoke check loaded the production app successfully.
- Injected an `elb:auth-session-issue` event in the production browser session and verified the session banner plus `Sign in again` action render.
