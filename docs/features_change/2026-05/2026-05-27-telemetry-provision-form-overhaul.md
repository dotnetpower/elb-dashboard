# Telemetry > Provision form — validation, lookup, safety, and parity

## Motivation

The "Provision a resource" form in Settings → Telemetry was a thin pass-through
to the backend `POST /api/settings/app-insights/provision` route, with no
client-side validation, no region helper, no existence preview, no confirmation
prompt, and no way to set the workspace's separate resource group or Log
Analytics retention. The 2026-05-27 walk-through identified 27 specific gaps.

## User-facing change

* All five existing fields now validate inline against the same regexes the
  backend enforces (subscription GUID, resource group, name, region). Invalid
  values render in red beneath the field and disable the submit button with a
  tooltip explanation.
* **Region** is now an `<input list="…">` backed by a `<datalist>` of the
  common Azure region slugs (28 entries). Free-text custom regions still work
  for unusual GAs, but typos like "koreacental" are caught before round-trip.
* A new optional **Workspace resource group** field lets operators put the Log
  Analytics workspace in a central observability RG rather than the App
  Insights RG.
* A new **Log Analytics retention** dropdown exposes the previously-hardcoded
  30-day value with 7 / 14 / 30 / 60 / 90 / 120 / 180 / 270 / 365 / 550 / 730
  options (Azure-supported set). Default stays at 30.
* The form is wrapped in `<form onSubmit>` so the Enter key now submits.
* The submit button is disabled until every field validates; the Loader2 icon
  now spins (re-uses the existing `spin` keyframe in `glass.css`).
* The whole form is `<fieldset disabled={busy}>` while the task runs so the
  user cannot mutate inputs mid-task.
* Clicking submit produces a `window.confirm` summary listing RG, AI name,
  region, workspace name (with cross-RG callout) and retention before any ARM
  call goes out. The dialog also notes that PerGB2018 ingestion charges may
  apply.
* A new **Reset to defaults** button restores the env-aware defaults (now
  `appi-${envName}` / `log-${envName}` derived from the workload RG, with the
  hard-coded `…-elb-dashboard` fallback).
* A debounced "does this name already exist?" check fires whenever the
  subscription / RG / AI name are all valid — UI shows
  *Checking…* → *Will reuse existing* / *Name is available* / *409 multiple
  matches*. This uses the existing `POST /api/settings/app-insights/lookup`
  route, no new backend surface needed.
* When the underlying Celery task finishes successfully, the form **auto-
  collapses** and the parent renders a persistent success line:
  *"Provisioning finished. App Insights component created;
  Log Analytics workspace reused. Server telemetry applied."*
* Region-mismatch warning: when the chosen region differs from the workload
  region recorded by the Setup Wizard, the hint line turns amber and explains
  the cross-region Container Insights latency cost.
* Every field now has an explicit `id` ↔ `htmlFor` pairing, `required` /
  `aria-required`, `aria-invalid`, and `autocomplete="off"`.
* `TaskStatusLine` now renders a thin progress bar driven by the
  `step / total_steps` payload that `publish_progress` was already sending.
  Provision shows three steps (workspace → component → server-apply).

## API / IaC diff summary

### Backend

* `POST /api/settings/app-insights/provision` now also accepts:
  * `workspace_resource_group?: string` (already plumbed through to the task,
    but exercised by tests for the first time).
  * `retention_days?: int` — validated against the discrete Azure-supported
    set `{7, 14, 30, 60, 90, 120, 180, 270, 365, 550, 730}`; anything else is
    a `400`.
* `provision_app_insights` Celery task now:
  * Takes a `retention_days: int | None` kwarg and forwards it to
    `ensure_log_analytics_workspace`.
  * Performs a quick `get_workspace` / `get_application_insights` lookup
    **before** the ensure calls so the result envelope can include
    `workspace_created: bool` and `component_created: bool`. The SPA uses
    these to render "created" vs "reused" in the success line.
  * Result envelope additions: `workspace_created`, `component_created`. The
    pre-existing keys (`workspace`, `component`, `connection_string`,
    `deployment_apply`) are unchanged so older clients still work.
* `api/tests/test_settings_app_insights.py`:
  * `test_provision_enqueues_celery_task_and_returns_id` now also asserts the
    optional kwargs default to `None`.
  * New `test_provision_accepts_workspace_rg_and_retention_days`.
  * New `test_provision_rejects_invalid_retention_days`.

### Frontend

* `AppInsightsProvisionRequest` adds `retention_days?: number`.
* `SettingsPanel.tsx`:
  * `ProvisionFormState` adds `workspace_resource_group: string` and
    `retention_days: number`.
  * `TaskState` adds `step?: number` and `totalSteps?: number`; `usePollTask`
    captures both from the `progress` payload and `TaskStatusLine` renders a
    matching `<div role="progressbar">`.
  * New `ProvisionField`, `validateProvisionFields`, `validateProvisionForm`,
    `deriveEnvName` helpers (all module-local).
  * The standalone `Field` primitive is unchanged; the form uses
    `ProvisionField` instead to get explicit `htmlFor`, error slot, and
    optional warning-tone hint.

### IaC

None.

## Validation evidence

* `uv run pytest -q api/tests/test_settings_app_insights.py` → 12 passed
  (added 3 over the previous 10).
* `uv run pytest -q api/tests` → 1527 passed (no regressions).
* `uv run ruff check api` → All checks passed!
* `cd web && npm run build` → built in 6.77 s (no TS errors).

## Out of scope (intentionally deferred)

* **Custom tag editor in the form** — charter §12 defines a standard tag set
  that IaC stamps onto every resource; per-resource user-supplied tags would
  diverge from that convention.
* **Per-field font-family / monospace exception** — the cosmetic concern that
  `INPUT_STYLE` is monospace for all fields. Defer; not blocking.
* **Native "Cancel" inside the form** vs the existing row-level "Hide form" —
  the two controls have the same effect (collapse without submitting), so a
  duplicate Cancel button would only add visual noise.
