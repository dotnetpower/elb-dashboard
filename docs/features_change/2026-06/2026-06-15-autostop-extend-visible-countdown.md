---
title: Auto-stop Extend shows a live countdown
description: Pressing "Extend 30 min" now surfaces a visible countdown to the grant expiry instead of an empty "paused by Extend" note.
tags:
  - user-guide
  - blast
---

# Auto-stop "Extend 30 min" now shows the extended time

## Motivation

Pressing **Extend 30 min** on the cluster auto-stop panel persisted the grant
(`extend_until = now + 30 min`) correctly, but the idle evaluator returned the
`extended` verdict with an empty `next_stop_at` and `seconds_until_stop = 0`.
The SPA renders its countdown from `next_stop_at` / `seconds_until_stop`, so the
panel collapsed to the muted "Auto-stop armed · Auto-stop is paused by Extend"
note with no visible time. From the operator's point of view the extension
looked like a no-op — the 30 extra minutes were never shown.

## User-facing change

While an Extend grant is active, the auto-stop panel now shows a live
countdown ("Stops in 29:59 …") that ticks down to the grant expiry, and
re-pressing Extend pushes it back to 30:00. The grant expiry is the earliest
the cluster can be stopped; the first evaluator tick after it passes
re-evaluates the idle clock as before.

## API / IaC diff summary

- `api/services/auto_stop_evaluator.py`: the `is_extended` branch now populates
  `next_stop_at = extend_until` and `seconds_until_stop = max(0, extend_until - now)`
  instead of returning an empty deadline. Verdict stays `keep`, reason stays
  `extended`; the beat driver (acts only on `stop`) is unaffected.
- `IdleDecision.next_stop_at` docstring updated to document the extended-grant
  case.
- No route, schema, or IaC change — the `/api/aks/autostop/status` response
  shape is unchanged; only the populated values differ for an active grant.

## Validation evidence

- `uv run pytest -q api/tests/test_auto_stop_evaluator.py api/tests/test_aks_autostop_route.py api/tests/test_auto_stop.py` → 78 passed.
- `test_extend_overrides_idle` now asserts the extended decision carries a
  non-empty `next_stop_at` and ~10 min `seconds_until_stop`.
- `uv run ruff check api/services/auto_stop_evaluator.py` → clean.
