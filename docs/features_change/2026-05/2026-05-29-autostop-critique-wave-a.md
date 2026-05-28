# 2026-05-29 — Auto-stop critique fixes wave A (#9.x / #10–#19)

## Motivation

A self-critique pass on the auto-stop / NCBI surfaces flagged 11 hardening
items grouped under [GitHub issues #9–#19](https://github.com/dotnetpower/elb-dashboard).
This change ships waves 1-6 (NCBI quota + auto-stop route + evaluator +
idle_autostop beat + file backend + AutoStopPanel UX) as one coherent
backend+frontend fix so the round can close together.

## User-facing change

- **Toast on auto-stop save / extend failure (#9.1).** Previously, if the
  backend rejected the toggle (RBAC mismatch, 503, …), the UI silently
  stayed "checked" while the server kept the old value. Now the panel
  rolls the optimistic toggle back to the last-committed server state
  and surfaces the error message via the global toast.
- **No more empty `auto_stop.json.lock` / `auto_warmup.json.lock` sentinels (#14).**
  The local-dev file backend stops creating a sibling `.lock` file every
  time it saves. Replaced the `fcntl.flock` cross-process lock with an
  in-process `threading.Lock` keyed by state file path (file backend is
  always single-process — Container Apps uses the Table backend).
- **AutoStopPanel stops polling when no transition is possible (#9.8).**
  When the cluster is stopped, or when auto-stop is disabled and the
  verdict is already `"disabled"`, the SPA stops the 60 s status poll
  entirely. It resumes automatically on the next user toggle or the
  next `clusterIsRunning` flip.
- **AutoStopPanel converges instantly when the cluster stops (#17).**
  When the parent flips `clusterIsRunning` from true to false (Start/Stop
  button, idle auto-stop tick, external `az` CLI), the panel invalidates
  the status query so the warn-banner / countdown disappears immediately
  instead of waiting up to a minute for the next poll.
- **Concurrent auto-stop toggle no longer clobbers the user's edit (#12).**
  The idle_autostop beat used to read the preference at decision time
  and write it back wholesale at rollback time. If the user toggled the
  cluster off between the read and the write, the toggle would silently
  reappear. The rollback path now re-fetches the row and only writes
  `last_stop_at` / `last_stop_reason` onto the fresh copy.
- **"No jobs ever" auto-stop verdict is now stable (#9.2).**
  The evaluator used to anchor its "X minutes since enrolment" message
  on `updated_at`, which moved every time the beat ticked a warning →
  the displayed elapsed clock got reset every minute. It now anchors on
  the new `created_at` field (stamped on first save), so the dashboard
  shows a monotonically increasing "Idle for Nm" message.
- **NCBI per-caller rate limit is now correct under dev-bypass and empty
  oid (#10, #11, #13, #19).**
  Multiple local dashboards running under `AUTH_DEV_BYPASS=true` no
  longer share one quota bucket. Real callers without an oid get a
  401 instead of being silently bucketed as "anonymous". The bucket
  map is now `OrderedDict`-LRU bounded at 4096 keys, and a 5xx error
  refunds the timestamp the caller just spent so retries are not
  double-billed.

## API / IaC diff summary

### Backend (`api/`)

| File | Change |
|---|---|
| [api/routes/ncbi.py](../../../api/routes/ncbi.py) | LRU-bounded caller buckets, dev-bypass UPN namespace, empty-oid 401, refund on 5xx (#10, #11, #13, #19) |
| [api/routes/aks/autostop.py](../../../api/routes/aks/autostop.py) | `async def` + `asyncio.to_thread` for SDK calls, `threading.Event` singleflight, `_pref_response` returns `DEFAULT_COOLDOWN_MINUTES` instead of bare `None`, oid masking (#9.4, #9.5, #9.7, #9.9, #9.10) |
| [api/services/auto_stop.py](../../../api/services/auto_stop.py) | New `created_at` field on `AutoStopPreference`, `_file_backend_lock` (per-path `threading.Lock`) replaces `fcntl.flock` + `.lock` sentinel (#9.2 schema, #14) |
| [api/services/auto_warmup.py](../../../api/services/auto_warmup.py) | Mirror of `_file_backend_lock` to eliminate `auto_warmup.json.lock` (#14 sibling) |
| [api/services/auto_stop_evaluator.py](../../../api/services/auto_stop_evaluator.py) | `raw = pref.created_at or pref.updated_at` anchor for "no jobs ever" (#9.2) |
| [api/tasks/azure/idle_autostop.py](../../../api/tasks/azure/idle_autostop.py) | `_batch_power_states` now returns `(states, summary)`; per-RG try/except with WARNING-level structured log; rollback re-fetches preference + writes only stop fields (#9.3, #12, #15) |

### Frontend (`web/`)

| File | Change |
|---|---|
| [web/src/components/ClusterItem/AutoStopPanel.tsx](../../../web/src/components/ClusterItem/AutoStopPanel.tsx) | `useToast` integration, `lastCommittedRef` for optimistic-rollback, refetchInterval returns `false` for `!enabled && verdict==="disabled"`, `useEffect` invalidating statusKey on `clusterIsRunning` true→false (#9.1, #9.8, #17) |

### Tests

| File | Change |
|---|---|
| [api/tests/test_ncbi_nuccore.py](../../../api/tests/test_ncbi_nuccore.py) | 9 new tests for bucket key namespacing, LRU cap, refund, empty-oid 401, module-load guard (#10, #11, #13, #19) |
| [api/tests/test_auto_stop.py](../../../api/tests/test_auto_stop.py) | `test_file_backend_save_does_not_create_lock_sentinel` — guards #14 |
| [api/tests/test_auto_warmup.py](../../../api/tests/test_auto_warmup.py) | Same sentinel guard for warmup file backend |
| [api/tests/test_auto_stop_task.py](../../../api/tests/test_auto_stop_task.py) | `_batch_power_states` tests updated for new `(states, summary)` return shape; `rg_groups`/`rg_failed`/`failed_rgs` assertions added |

### IaC

No infra changes in this wave.

## Validation evidence

```text
$ uv run pytest -q api/tests
.......................................................................  [100%]
1883 passed, 3 skipped in 36.11s

$ cd web && npm test -- --run
 Test Files  54 passed (54)
      Tests  425 passed (425)
   Duration  4.86s

$ uv run ruff check api
All checks passed!

$ cd web && npm run build
✓ built in 7.04s
```

Focused suites used during development:

- `uv run pytest -q api/tests/test_ncbi_nuccore.py` → **52 passed**
- `uv run pytest -q api/tests/test_aks_autostop_route.py` → **17 passed**
- `uv run pytest -q api/tests/test_auto_stop.py api/tests/test_auto_stop_task.py api/tests/test_auto_warmup.py api/tests/test_auto_stop_evaluator.py` → **66 passed**

## Self-review

- Consumer search for `_batch_power_states` returned 2 direct call sites
  in `test_auto_stop_task.py`; both updated to unpack `(states, summary)`.
- Consumer search for `AutoStopPreference.created_at` confirmed
  backward-compat default (`""` → falls back to `updated_at` until next
  save).
- Consumer search for `_save_file` / `_state_file` in both
  `auto_stop.py` and `auto_warmup.py` confirmed no caller depends on a
  `.lock` sentinel file existing.
- Frontend: `AutoStopPanel.tsx` `useToast` import is the same hook
  used elsewhere via `web/src/main.tsx` `ToastProvider` — no new
  provider wiring needed.
- Lint + build clean. Full backend + frontend suites green.
