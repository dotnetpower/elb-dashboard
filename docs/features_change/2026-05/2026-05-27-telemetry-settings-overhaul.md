# Telemetry Settings panel — UX, safety, and parity overhaul

## Motivation

The Telemetry tab in the global Settings panel had several latent footguns and
gaps surfaced during a walk-through review:

* "Send test event" stayed enabled while telemetry was off or no connection
  string was configured — clicking it produced a confusing error toast.
* The toggle could be flipped to **on** with no connection string anywhere,
  leaving the UI claiming "Browser telemetry starts immediately" when in fact
  the SDK never initialised.
* Typing a valid connection string into the override field **silently** posted
  a Container App template update to api / worker / beat after a 900 ms
  debounce. The user had no chance to review or cancel, and there was no way
  to undo the change short of re-deploying.
* The "Effective source" badge rendered the raw enum value (`NONE`, `user`,
  `deployment`) with no friendly hint.
* No way existed to remove a previously applied connection string from the
  server sidecars — operators had to redeploy.
* No portal link, copy button, doc link, server-side revision indicator, or
  validation feedback for the override field.

## User-facing change

* The browser telemetry toggle is **disabled** until either a deployment
  connection string is detected or the operator enters a complete override
  value. Trying to flip it on without one produces an inline explanation
  instead of silently storing `telemetryEnabled = true`.
* "Send test event" is disabled (with a tooltip) unless the App Insights JS
  SDK is actually active. The placeholder error toast is gone.
* "Effective source" is now a friendly sentence-case label with an icon and
  three colours (`Browser override · active`, `Deployment · active`,
  `Browser override · idle`, `Deployment · idle`, `Not configured`) and a hint
  line under it explaining what each state means.
* The connection-string override now has:
  * Inline validation (border turns green when `InstrumentationKey=` +
    `IngestionEndpoint=` are both present, warning amber otherwise).
  * `aria-invalid` + status line while the value is incomplete.
  * A **Copy** button next to the show/hide eye toggle. The eye toggle now
    reports `aria-pressed` for screen readers.
* Auto-apply is removed. An explicit **Apply to server sidecars** button takes
  its place — disabled when the value is malformed, when an apply task is
  already running, or when the current value already matches what is on the
  server. Its tooltip explains each disabled state.
* A new **Clear server override** button (with a `window.confirm` prompt)
  enqueues a task that removes `APPLICATIONINSIGHTS_CONNECTION_STRING` from
  api / worker / beat and rolls a new Container App revision. Disabled when
  there is nothing to clear.
* A new always-visible **Server sidecars** row shows the last applied revision
  name and the trailing 8 characters of the InstrumentationKey, so an operator
  can confirm what the server-side configuration actually carries.
* The Provision row shows the planned workspace / component / RG / region
  **before** the operator clicks "Open form".
* When the subscription + RG + component name are known, a deep link "Open in
  Azure Portal" takes the operator straight to the App Insights resource
  Overview blade. Otherwise it falls back to a generic browse link.
* The hint text now explicitly mentions that frontend and terminal sidecars
  are intentionally **not** updated (they have no telemetry surface), and
  links to the Microsoft Learn docs page for App Insights connection strings.

## API / IaC diff summary

### Backend

* New `POST /api/settings/app-insights/clear` route — auth-gated, no body.
  Enqueues `api.tasks.azure.clear_app_insights_from_deployment`. Returns the
  same `{task_id, status, statusQueryGetUri}` envelope as the existing
  `/apply` route.
* New `clear_app_insights_from_deployment` Celery task. Idempotent — when
  `CONTAINER_APP_NAME` is unset (local dev / pytest) it returns
  `{deployment_clear: {status: "skipped", reason: "container_app_env_missing"}}`
  without touching ARM.
* New `clear_app_insights_connection_string` helper in
  `api/services/upgrade/aca_template.py` — symmetric to
  `apply_app_insights_connection_string`. Returns `(poller, removed_count)`.
* New `_remove_env_var_from_containers` template mutator in the same module.

### Frontend

* `settingsApi.clearAppInsightsFromDeployment()` typed client.
* Two new `Preferences` keys persisted in `localStorage["elb-prefs"]`:
  `appInsightsLastAppliedRevision` and `appInsightsLastAppliedKeyTail`. Both
  default to empty string; `Preferences.__v` stays at 1 because the existing
  spread-with-defaults migration already absorbs new optional keys.
* `Badge`, `Toggle`, and `IconButton` primitives in `SettingsPanel.tsx` now
  accept `tone="warning"`, `disabled`, `describedBy`, `pressed`, and `title`
  props respectively. No external consumers changed shape.

### IaC

None. The new Celery task uses the same Container Apps RP write path that
`apply_app_insights_connection_string` already exercises; no new RBAC, no new
secrets, no Bicep changes.

## Validation evidence

* `uv run pytest -q api/tests/test_settings_app_insights.py api/tests/test_upgrade_aca_template.py` → 17 passed.
* `uv run pytest -q api/tests` → 1525 passed (no regressions).
* `uv run ruff check api` → All checks passed!
* `cd web && npm run build` → built in 12.52 s (no TS errors).

## Out of scope (intentionally deferred)

* **Server-side test event** — would require pulling
  `azure-monitor-opentelemetry` (or the legacy `opencensus-ext-azure`) into
  the api image just for one button. Browser test event covers the
  "is my connection string valid?" question for now.
* **Live metrics counter** — needs an authenticated query against the App
  Insights data plane, which is its own auth dance. Out of scope for this PR.
* **Per-section reset** — the global Reset button in the panel footer already
  resets the telemetry prefs alongside theme/preview prefs.
