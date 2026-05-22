# 2026-05-22 — Workspace diagnostics hardening

Self-critique follow-up to the two earlier changes in this folder:

* [2026-05-22-workspace-degraded-diagnostics.md](./2026-05-22-workspace-degraded-diagnostics.md)
* [2026-05-22-me-subscriptions-visibility.md](./2026-05-22-me-subscriptions-visibility.md)

Found and fixed five issues that survived the original implementation
review.

## H1 — Banner went silent when ARM subscription listing itself failed

**Problem.** When `armProxyApi.listSubscriptions()` errored (the most
common state: `az login` missing/expired/ARM consent not granted),
`SubscriptionPicker.invalidValue` fell through to `false` and the
WorkspaceDiagnosticsBanner's `subsQuery.data` was `undefined`, so
`visibleSubscriptionInfo` stayed non-degraded. Cards still degraded with
`auth_wrong_tenant` / `not_found`, but the banner missed the most
actionable signal of all: "you are not signed in to Azure."

**Fix.** New synthetic reason `subscriptions_unavailable` at the top of the
severity order. The banner now synthesises it from either
`subsQuery.isError` or `subsQuery.data.length === 0`. UI labels: card chip
**Sign in to Azure**, banner title *"Sign in to Azure to load workspace
data"*, body points at `az login --tenant <…>` and the Reset workspace
button.

## H2 — Wrong-tenant classifier relied on English substring matching

**Problem.** [`api/routes/monitor/common.py::_classify_exception`](../../../api/routes/monitor/common.py)
matched `"InvalidAuthenticationTokenTenant"` and friends in the raw
exception text. Stable today, but a single Azure wording change would
silently demote `auth_wrong_tenant` to `unauthorized`.

**Fix.** Prefer the structured `HttpResponseError.error.code` (which Azure
guarantees and SDKs surface as `exc.error.code`). The new
`_looks_like_wrong_tenant` helper:

  1. Checks `error.code` against an allow-list of known tenant/issuer
     codes: `InvalidAuthenticationTokenTenant`,
     `InvalidAuthenticationToken`, `AuthorizationFailed`.
  2. Treats `AuthorizationFailed` (which is also returned for plain
     missing-role 401/403) as wrong-tenant **only** when the message body
     also names the issuer marker, so a missing-role 401 still maps to
     `unauthorized`.
  3. Falls back to the original substring matchers when `error` is `None`.

Four new test cases in `test_monitor_graceful.py` pin the behaviour.

## M1 — Reset workspace had no confirm step

**Problem.** Single click cleared `localStorage` and reloaded the page. A
misclick during wizard input would silently throw away that input.

**Fix.** Wired the existing `ConfirmDialog` into the banner. The reset
button now opens a modal with "*This clears the saved Subscription /
Resource Group / ACR / Storage names and re-opens the setup wizard. Any
in-flight wizard input will be lost.*" and only commits on Confirm.

## M2 — Dismissed banner state used a `useState(0)+force` re-render hack

**Problem.** The banner read `dismissed = readDismissed(reason)` directly
from `localStorage` on every render and used a throwaway counter to force
re-renders on dismiss. The state was not React-managed and was not synced
across tabs.

**Fix.** New `useDismissed(reason)` hook wraps `useSyncExternalStore` with
two listener sources:

  1. an in-process subscriber set (`dismissListeners`) the write helpers
     notify after `localStorage.setItem` / `removeItem`,
  2. the browser `storage` event, scoped to keys starting with the
     dismissal prefix, so a Reset workspace in tab A unhides the banner in
     tab B.

The hook returns `false` (and still subscribes) when the reason is null,
so React's concurrent mode rules are respected. `useState(0)` is gone.

## M3 — `test_me_requires_caller` used `importlib.reload(api.main)`

**Problem.** Reloading the FastAPI app module from inside one test left
brittle global state. Any test in the suite that had already imported
`api.main` could see stale routers between the reload calls.

**Fix.** `require_caller` already reads `AUTH_DEV_BYPASS` lazily on every
call. Replaced the reload dance with a plain
`monkeypatch.setenv("AUTH_DEV_BYPASS", "false")` and a single
`client.get("/api/me")` assertion.

## Validation

```text
$ uv run pytest -q api/tests/test_me_route.py api/tests/test_monitor_graceful.py api/tests/test_smoke.py api/tests/test_route_contracts.py
103 passed in 5.74s

$ uv run ruff check api/routes/me.py api/routes/monitor/common.py api/tests/test_me_route.py api/tests/test_monitor_graceful.py
All checks passed!

$ cd web && npx vitest run src/utils/monitorDegraded.test.ts
Test Files  1 passed (1)
     Tests  17 passed (17)

$ cd web && npx tsc --noEmit -p tsconfig.json
(no output)

$ cd web && npm run build
✓ built in 8.67s
```

## Knowingly deferred (Low severity, separate PR if needed)

* **L2** — `SidecarsCard`, `TerminalCard`, `JobCard` still ignore
  `degraded_reason`. Consistent treatment is nice-to-have but their
  failure modes already have bespoke UI.
* **L3** — Banner body text contains markdown backticks
  (`` `az login --tenant …` ``) that render as literal text. Switching the
  banner to a tiny markdown renderer is a larger UX change than this
  hardening PR warrants.
* **L4** — `web/src/api/me.ts` typed client is still unused. Wiring it
  into the SPA boot path would let us drop the duplicated
  `arm-subscriptions` round-trip but TanStack Query already dedupes the
  call so the win is small.
* **L5** — Bonus a11y: the banner now uses `role="status"` +
  `aria-live="polite"` instead of `role="alert"` so it doesn't interrupt
  screen-reader users on every dashboard render.
