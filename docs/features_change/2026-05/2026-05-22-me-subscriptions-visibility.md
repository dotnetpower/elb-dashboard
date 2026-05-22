# 2026-05-22 — `/api/me` visible subscriptions + invisible-subscription banner

Companion change to [2026-05-22-workspace-degraded-diagnostics.md](./2026-05-22-workspace-degraded-diagnostics.md).
Closes the remaining gap: detect when the `subscriptionId` saved by the
setup wizard is no longer in the list of subscriptions the current Azure
credential can see.

## Motivation

The previous change covered backend-side degraded reasons (ARM 401/403/404
classified into `auth_wrong_tenant` / `unauthorized` / `forbidden` /
`not_found`). It did **not** cover the very first failure mode users hit on
machines with multiple Azure profiles:

> The wizard saved `subscriptionId = X`. The user then switched their `az`
> profile (e.g. `az-jungha`). `X` is no longer visible to the current
> credential, but the SPA's Subscription dropdown silently renders a blank
> option and the SPA keeps calling `/api/monitor/*` with `X`.

Both ARM endpoints (`/api/arm/subscriptions`) and the monitor router already
existed, but no component connected them to the diagnostics banner.

## User-facing change

* The Subscription chip in the dashboard header now renders an amber
  warning indicator when the saved `subscriptionId` is not in the list
  returned by `/api/arm/subscriptions`. The chip's `<option>` carries the
  `(not visible)` suffix so the value is at least readable.
* `WorkspaceDiagnosticsBanner` adds a new reason class
  `invisible_subscription` that triggers the banner immediately (even with
  no other degraded cards) and surfaces a danger-toned guidance message
  pointing at the dropdown and the **Reset workspace** button.
* `/api/me` now returns the same subscription list as
  `/api/arm/subscriptions` plus a non-fatal `subscriptions_error` field
  when ARM listing fails. The SPA can use this to render the picker on
  first load without a second round-trip; this PR adds the typed client and
  leaves the optimisation to a follow-up.

## Backend change

`api/routes/me.py` extracts `_list_visible_subscriptions()` (best-effort
ARM list) and augments the response shape:

```jsonc
{
  "object_id": "…",
  "tenant_id": "…",
  "upn": "…",
  "subscriptions": [
    { "subscriptionId": "…", "displayName": "…", "tenantId": "…", "state": "Enabled" }
  ],
  "subscriptions_error": "AzureError: …"    // only on failure
}
```

The `subscriptions_error` field is omitted on success. Identity claims are
always returned even if ARM listing fails — the SPA can still render the
profile menu without breaking.

## SPA change

* `web/src/api/me.ts` — new typed client `meApi.get()`.
* `web/src/components/SubscriptionPicker.tsx`:
  * Computes `invalidValue` by comparing the saved `value` against
    `armProxyApi.listSubscriptions()` (which TanStack Query already caches
    for both the wizard and the picker — no extra network calls).
  * Adds an `(not visible)` option for the saved value so the `<select>`
    doesn't silently render a blank choice.
  * Renders an `AlertTriangle` glyph + `cfg-chip--invalid` outline in the
    compact chip variant and an inline warning under the wizard variant.
* `web/src/utils/monitorDegraded.ts`:
  * Adds `invisible_subscription` to `DegradedReason` (synthetic — never
    returned by the backend, synthesised by the banner).
  * Adds it to the severity ordering and the `show` predicate.
  * New banner copy ("Saved subscription is not visible …").
* `web/src/components/WorkspaceDiagnosticsBanner.tsx`:
  * Re-uses the same `arm-subscriptions` query as the picker (deduped by
    TanStack Query).
  * Synthesises a `DegradedInfo` for the saved subscription when it isn't
    in the visible list and feeds it into `aggregateDiagnostics`.
  * Banner tone bumped to `danger` for `invisible_subscription` (same as
    `auth_wrong_tenant`).

## Validation

```text
$ uv run pytest -q api/tests/test_me_route.py api/tests/test_monitor_graceful.py api/tests/test_smoke.py api/tests/test_route_contracts.py
99 passed in 6.88s

$ uv run ruff check api/routes/me.py api/tests/test_me_route.py
All checks passed!

$ cd web && npx vitest run src/utils/monitorDegraded.test.ts
Test Files  1 passed (1)
     Tests  15 passed (15)

$ cd web && npm run build
✓ built in 8.28s
```

Manual smoke (about to be re-verified live):

1. With `az` logged into tenant A and `localStorage` holding a
   `subscriptionId` from tenant B, the header chip shows an amber warning
   and the banner reads "Saved subscription is not visible". **Reset
   workspace** clears the storage and re-runs the wizard.
2. Switching to a visible subscription in the dropdown immediately removes
   the warning chip and dismisses the banner.

## Out of scope for this PR

* Wiring the new `meApi` client into the SPA boot path to skip the
  separate `/arm/subscriptions` round-trip (purely an optimisation —
  TanStack Query dedupes the duplicate call anyway).
* SetupWizard inline ARM probe for RG/ACR/Storage names. The monitor
  `degraded_reason: not_found` plus the workspace banner already catch the
  most common stale-settings case; full pre-validation can be added as a
  follow-up if it proves necessary.
