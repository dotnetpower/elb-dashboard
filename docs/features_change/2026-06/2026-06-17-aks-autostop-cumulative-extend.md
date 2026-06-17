---
title: AKS Auto-Stop Cumulative Extension Fix
description: Extend button now adds 30 minutes to existing deadline; backend behavior now matches frontend UI promise.
tags: [aks, autostop, ui]
---

## Motivation

The browser UI button label changed in a prior turn from "Extend 30 min" to "+30 min", implying
cumulative (additive) extension. However, the backend `extend_auto_stop_preference()` function
was resetting the deadline to an absolute time (`now + 30 min`) instead of extending the
existing deadline. This semantic mismatch meant:

- **First press**: Sets deadline to now + 30 min ✓
- **Second press while active**: Resets deadline to now + 30 min (losing the earlier +30) ✗

The new UI promised "+30 min" (add to existing), but the backend was implementing "set to 30 min".

## User-Facing Change

The "Extend" button in the AKS cluster auto-stop control now correctly adds 30 minutes to the
existing deadline if an extension is already active:

- **Active extension (deadline in future)**: Pressing extend adds 30 min to that deadline.
  - Example: deadline is 20 min away → press extend → deadline moves to 50 min away.
- **Expired extension (deadline in past)**: Pressing extend resets and starts fresh (now + 30 min).
  - Graceful degradation: prevents growing the deadline indefinitely when the UI is inactive.

Functionally identical to the old behavior for first-press (extends never before pressed, or
long-expired). Difference is **second+ presses while active now accumulate** instead of resetting.

## API & Backend Diff

**Route contract unchanged**: POST `/api/aks/autostop/extend` request and response shape
are identical. Request body still `{ minutes: 30 }`, response still includes `extend_until` ISO8601.

**Service logic change**:

```python
# OLD (lines 304-308 before)
next_pref.extend_until = (
    datetime.now(UTC) + timedelta(minutes=grant)
).isoformat(timespec="seconds")

# NEW (lines 308-313 after)
now = datetime.now(UTC)
current_deadline = _parse_iso(next_pref.extend_until)
base_deadline = (
    current_deadline
    if current_deadline is not None and current_deadline > now
    else now
)
next_pref.extend_until = (base_deadline + timedelta(minutes=grant)).isoformat(
    timespec="seconds"
)
```

**File**: `api/services/auto_stop.py`, function `extend_auto_stop_preference()`

The function now:
1. Reads the current `extend_until` deadline from the stored preference.
2. Checks if it is in the future (still active).
3. If active: adds the grant (30 min) to the existing deadline → cumulative.
4. If expired or empty: resets to now + grant → graceful fallback.

**Concurrency safety**: The Compare-And-Swap (CAS) retry loop remains unchanged, ensuring
atomic updates even when two extend requests race.

## Validation

### New Test Cases
- **`test_extend_auto_stop_preference_adds_to_active_grant()`** (unit, `api/tests/test_auto_stop.py`)
  - Seed `extend_until = now + 20 min`, call extend(30), verify result is 45–55 min away.
  - Confirms cumulative semantics for active deadline.
- **`test_extend_auto_stop_preference_ignores_expired_grant()`** (unit, `api/tests/test_auto_stop.py`)
  - Seed `extend_until = now - 20 min` (past), call extend(30), verify result is 25–35 min away.
  - Confirms graceful reset for expired deadline (not 50+ min).
- **`test_extend_route_adds_to_active_grant()`** (HTTP, `api/tests/test_aks_autostop_route.py`)
  - Full route stack: PUT preference, seed deadline, POST `/api/aks/autostop/extend`, verify
    response body `extend_until` is cumulative.
  - Confirms browser-facing endpoint semantics.

**Test Results**: All 47 auto-stop tests pass (focused suite); 2639 total backend tests pass.

### Backward Compatibility
- No new fields, no removed fields. Existing deployments gracefully degrade (expired grants
  reset to now + grant, same as before).
- No breaking API change.
- Stored preferences remain ISO8601 compatible.

## Verification Evidence

```bash
$ uv run pytest -q api/tests/test_auto_stop.py api/tests/test_aks_autostop_route.py -v
======================== 47 passed in 70.89s ========================

$ uv run ruff check api
All checks passed!
```

Consumer grep confirms no other code expects absolute (non-cumulative) `extend_until` semantics;
evaluator already interprets deadline as future-relative.
