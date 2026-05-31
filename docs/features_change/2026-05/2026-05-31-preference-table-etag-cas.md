# Preference tables — ETag-based optimistic concurrency

**Issue**: [#21](https://github.com/dotnetpower/elb-dashboard/issues/21)
**Date**: 2026-05-31
**Layer**: backend / preferences

## Motivation

`AutoStopPreference` and `AutoWarmupPreference` rows in the Azure Table backend
(and their JSON fallback for local dev) were last-writer-wins. The Stage 1
audit found three concurrent writers in real traffic — the deploy task, the
scheduled idle evaluator (Celery beat), and the UI mutation — each loading the
row, mutating fields, and unconditionally `upsert`-ing it back. A snooze + an
auto-stop event landing in the same 10s window silently dropped one of them.

The same hazard existed in `AutoWarmupPreference.mark_auto_warmup_ready_state`,
which additionally had **no fresh-read before mutation** — even single-writer
races against a stale in-memory copy could clobber another tab's edit.

## What changed

Backend (`api/`):

- New shared primitive `api/services/preference_concurrency.py`
  - `PreferenceUpdateConflict(RuntimeError)` — raised by every preference
    write that loses an ETag race.
  - `cas_retry(attempt, max_attempts=5, operation="...")` — re-runs the
    attempt callable up to 5 times, logging INFO per retry and WARNING on
    exhaustion. Re-raises the last conflict after exhaustion.
- `api/services/auto_stop.py`
  - `AutoStopPreference.etag: str` (`default=""`, `compare=False`, `repr=False`).
    `to_dict()` excludes the etag so existing serialisers stay byte-identical.
  - `_save_table` now uses `MatchConditions.IfNotModified` when the in-memory
    etag is non-empty, raising `PreferenceUpdateConflict` on
    `ResourceModifiedError` and falling back to a fresh `upsert` on
    `ResourceNotFoundError`. Returns the new ETag.
  - `_save_file` raises `PreferenceUpdateConflict` if the disk content hash
    differs from the in-memory baseline. File-backend etag = sha256 of the
    canonical JSON (`json.dumps(..., sort_keys=True, separators=(",",":"))`).
  - `mark_auto_stop_event` wraps the read/mutate/save sequence in `cas_retry`;
    after exhaustion logs a warning and returns the in-memory fallback (the
    event itself is best-effort, not a hard error).
  - `extend_auto_stop_preference` wraps in `cas_retry` and lets the final
    conflict bubble, so the HTTP route surfaces a 409.
- `api/services/auto_warmup.py` — same pattern. `mark_auto_warmup_ready_state`
  now does a fresh-read + CAS retry (it had neither before).

Tests (`api/tests/`):

- New `api/tests/test_preference_etag.py` — 12 tests covering the happy path,
  the race retry path, exhaustion (rotates `idle_minutes` across 5 attempts so
  the ETag changes on every read), file-backend hash conflict, and the
  `mark_*` fallback path. Uses `monkeypatch.delenv("AZURE_TABLE_ENDPOINT")` +
  `monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))` to force the
  file backend.

## Backward compatibility

- `etag` is additive (`default=""`, `compare=False`); existing callers that
  ignore the field continue to work unchanged.
- `save_*_preference` return-value semantics unchanged in practice — the
  field added is a `str` that was previously absent. Equality-based tests
  remain green because `compare=False` excludes it from dataclass equality.
- Private `_save_table` / `_save_file` now return `str` instead of `None`.
  Grep across the repo confirmed no external caller reads those returns.

## Validation

- `uv run pytest -q api/tests/test_preference_etag.py` — 12 new passes.
- `uv run pytest -q api/tests/test_aks_autostop_route.py api/tests/test_auto_stop_task.py api/tests/test_auto_stop_evaluator.py` — 44 related passes (no regressions).
- Charter §11 SRP gate: new helper module owns one responsibility (CAS retry +
  shared conflict exception). The two service modules retain their existing
  responsibilities and gain the retry wrapper at the same layer as the rest of
  their persistence logic.

## Acceptance

- Two simultaneous writers against the same `(subscription, resource_group, cluster)`
  preference row no longer silently drop one update.
- The HTTP `extend_auto_stop_preference` route surfaces a 409 to the SPA when
  exhaustion happens; the SPA can refresh and retry.
- The background `mark_auto_stop_event` writer falls back gracefully (warning
  log + in-memory result) so a transient table conflict does not crash the
  Celery task chain.

## Out of scope

- Cross-table transactions (still unsupported in Azure Tables; the gate is
  per-row).
- Surfacing the conflict count in the dashboard. The WARNING log line is the
  observability surface for Stage 1; App Insights querying lives in operate
  docs if needed.
