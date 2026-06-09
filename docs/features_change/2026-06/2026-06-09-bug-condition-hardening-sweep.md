---
title: Hardening sweep — silent input, polling, and reconciler bugs
description: A batch of defensive fixes for numeric-input edge cases, overlapping async polls, a missing-blob artifact loop, silent subscription truncation, and a reconciler datetime crash.
tags:
  - blast
  - ui
  - architecture
---

# Bug-condition hardening sweep (2026-06-09)

A targeted hunt for the same *classes* of defect as the earlier round (silent
failures, ghost/stale data, fallbacks that fire on the wrong condition,
invisible background work). Ten distinct conditions were verified against the
code and fixed.

## Frontend

1. **`max_target_seqs` input dropped a deliberate `0`.**
   `parseInt(value, 10) || 100` silently replaced a user-entered `0` (falsy)
   with the default. Now parsed via `parseNumericInput`, which preserves `0`
   and only falls back on empty/NaN.
   ([AlgorithmParametersSection.tsx](../../../web/src/pages/blastSubmit/AlgorithmParametersSection.tsx))
2. **`evalue` input dropped a deliberate `0`** — same `|| 0.05` bug, same fix.
3. **`word_size` / `gap_open` / `gap_extend` sent `NaN` for whitespace.**
   `form.x ? parseInt(form.x, 10) : undefined` treated a whitespace string as
   truthy and emitted `NaN` (JSON-serialised to `null`) to the API. Now trimmed
   and dropped to `undefined` when not a finite integer.
   ([useSubmitMutation.ts](../../../web/src/pages/blastSubmit/useSubmitMutation.ts))
4. **Settings task poller fired overlapping requests.**
   `setInterval(async …)` does not await its callback, so a slow
   `/tasks/status` let a stale response clobber a fresh one. Added a re-entry
   guard so at most one poll is in flight.
   ([taskState.tsx](../../../web/src/components/settings/taskState.tsx))
5. **HTTP inspector poller had the same overlapping-request bug** — same
   re-entry-guard fix.
   ([HttpInspectorPanel.tsx](../../../web/src/components/cards/SidecarsCard/HttpInspectorPanel.tsx))

## Backend

6. **A "ready" artifact whose blob was deleted retried the 404 forever.**
   `read_json_artifact` did not catch `ResourceNotFoundError`, so a missing blob
   raised on every read and the state row stayed `ready`. Now it catches the
   404, flips the row to `failed` (`error_code="blob_missing"`) so
   `artifact_build_should_enqueue` re-bakes it, and returns `None`.
   ([job_artifacts.py](../../../api/services/job_artifacts.py))
7. **Subscription list truncated silently at the cap.**
   With >`ME_SUBSCRIPTIONS_LIST_LIMIT` subscriptions the picker showed an
   arbitrary subset with no signal. The helper now sets
   `subscriptions_error="subscriptions_truncated"` (surfaced as
   `subscriptions_error` on `/api/me`) so the SPA can warn the user.
   ([me.py](../../../api/routes/me.py))
8. **Upgrade reconciler could crash on a naive timestamp.**
   `clock() - datetime.fromisoformat(anchor)` raises `TypeError` (not
   `ValueError`, which the call sites guarded) if `anchor` is timezone-naive —
   crashing the whole reconcile tick and stranding every in-flight upgrade. A
   new `_parse_anchor` helper coerces naive timestamps to aware UTC at all seven
   parse sites; malformed strings still raise `ValueError` for the existing
   handling.
   ([reconciler.py](../../../api/tasks/upgrade/reconciler.py))

(Conditions 1–3 are three independent fields; condition 8 covers seven parse
sites — ten distinct failure conditions in total.)

## Validation

- Backend: `uv run pytest -q api/tests` → 3138 passed; `uv run ruff check api`
  clean. New tests: `test_read_json_artifact_marks_failed_when_blob_missing`
  (+gzip variant), `test_list_visible_subscriptions_flags_truncation`,
  `test_parse_anchor_coerces_naive_to_utc`, `test_parse_anchor_raises_value_error_on_garbage`.
- Frontend: `npm run build` clean; `npx vitest run src/pages/blastSubmit`
  → 194 pass, including new `numericInput.test.ts`.
