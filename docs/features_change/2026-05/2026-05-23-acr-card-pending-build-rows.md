# ACR card: surface in-progress builds after browser refresh

## Motivation

After kicking off an ACR Build from the dashboard, the per-image rows
correctly showed a "Building" indicator while the SPA still had the
in-memory `BuildResult[]` with `run_id`s returned by
`POST /api/acr/build-images`. But a browser refresh (or opening the
dashboard in a second tab) cleared that in-memory state, and the rows
fell back to showing the per-row **Build** button — even though the ACR
runs were still in `Queued` / `Started` / `Running`. The user could
inadvertently kick off a duplicate build because the UI hid the fact
that the previous one was still in flight.

Root cause: `api/services/monitoring.list_acr_repositories` only populated
`building_images` / `build_details` for ACR runs whose `output_images`
field was non-empty. ACR only populates `Run.output_images` after the
`push` step succeeds — `Queued` / `Started` / `Running` runs typically
have an empty `output_images` list, so the monitoring endpoint had no
way to tell *which* image was being built.

## User-facing change

* Every image row in the **Azure Container Registry** card now stays in
  the "Queued" / "Running" indicator (instead of switching back to a
  **Build** button) after a browser refresh, for as long as the ACR run
  is in flight.
* The card-level loader (`status="loading"` + hidden top-right **Build**
  button) continues to render via the existing `hasServerBuilding`
  derived state — now correctly fed by the new fallback.
* No new API surface; the existing `/api/monitor/acr` response shape is
  unchanged.

## How it works

1. `api/tasks/acr.build_images` captures the run id from
   `mgmt.registries.begin_schedule_run(...)` without blocking on the
   long-running build (reads the initial response via
   `poller.polling_method().resource()`), then writes a
   `(registry, run_id) -> {image, tag}` row to a new `acrbuildruns`
   Azure Table partition.
2. `api/services/monitoring.list_acr_repositories` loads that mapping
   at the start of each request. For any ACR run in
   `Queued` / `Started` / `Running` whose `output_images` is empty, it
   synthesises a `building_images` / `build_details` entry from the
   persisted mapping.
3. The same call best-effort prunes rows whose run has reached a
   terminal status (`Succeeded` / `Failed` / `Canceled` / `Error` /
   `Timeout`), so the table doesn't grow without bound.

## API / IaC diff summary

* **New module**: `api/services/acr_build_state.py` — pooled
  `TableClient` against an `acrbuildruns` table (env override
  `ACR_BUILD_STATE_TABLE`). Public surface:
  `record_pending_build`, `load_pending_builds`,
  `prune_terminal_builds`. All three are best-effort and swallow Azure
  failures.
* `api/tasks/acr/__init__.py`:
  * `_schedule_acr_build` now returns the queued `run_id`.
  * `build_images` records the (run_id, image:tag) mapping.
  * Per-image `results` row gains an optional `run_id` field (already
    in the TS types in `web/src/api/monitoring.ts`).
* `api/services/monitoring.py::list_acr_repositories` consults the
  mapping for in-progress runs with empty `output_images` and prunes
  terminal rows.
* No infra changes — the new table is auto-created via
  `TableServiceClient.create_table_if_not_exists` against the existing
  `AZURE_TABLE_ENDPOINT`.

## Validation

* `uv run pytest -q api/tests/test_acr_build_task.py api/tests/test_acr_monitoring_pending.py`
  — 6 tests pass (existing 2 + 4 new: `test_schedule_returns_run_id_from_initial_response`,
  `test_schedule_tolerates_missing_run_id`,
  `test_pending_runs_without_output_images_surface_as_building`,
  `test_succeeded_run_prunes_pending_entry`).
* `uv run pytest -q api/tests` — 1260 / 1260 passed.
* `uv run ruff check api` — clean.
* No frontend changes required (`web/src/components/cards/AcrCard` already
  switches each row to the "Building" indicator the moment its
  `(image:tag)` appears in `data.build_details`).
