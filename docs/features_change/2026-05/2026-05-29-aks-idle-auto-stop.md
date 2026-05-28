# 2026-05-29 — AKS idle auto-stop (opt-in cost saver)

## Motivation

AKS managed clusters keep billing for the **system node pool** + control
plane even when no BLAST job has run for hours. Pure node-pool
autoscaling cannot reach zero on the system pool, so a researcher who
provisions a cluster at 09:00 and walks away at 17:00 still pays for
~16 idle hours overnight (and ~64 hours over the weekend) unless they
remember to press *Stop cluster (saves cost)* by hand. The dashboard
already exposes `POST /api/aks/stop`; this change wires that capability
to an automatic, opt-in evaluator so the cluster stops itself once it
has been idle for a configurable window.

User research bias built into the design: a researcher walking back
from lunch to "my cluster is gone" is a worse experience than a small
unused-hours bill. The default window is **60 minutes** (long enough to
absorb lunch breaks, short enough to capture overnight idle), the
feature is **opt-in**, and the dashboard surfaces a pre-stop countdown
banner with a one-click **Extend** button.

## User-facing change

* New per-cluster "Auto-stop when idle for [60 ▾] minutes to save cost"
  toggle inside each cluster's expanded card on the dashboard. Choices
  are `15 / 30 / 60 / 120 / 240` minutes; default is **60 min**, default
  is **off** (opt-in).
* When the backend evaluator marks a cluster as nearly idle (≤ 15 min
  left, or the last quarter of the configured window — whichever is
  smaller), a calm amber **countdown banner** appears in the same card:
  > Auto-stop in **9m 43s** (≈ 14:12) · Idle window almost elapsed.
  > [ Extend 30 min ]
* "Extend 30 min" pushes the deadline out by 30 minutes via
  `POST /api/aks/autostop/extend`. The next evaluator tick honours the
  grant and re-renders the banner without a countdown until the grant
  expires.
* When auto-stop fires, the existing `aksApi.stop()` lifecycle path runs,
  so the cluster transitions through the same `Stopping…` UX the manual
  Stop button uses. The next BLAST submit gate already returns the
  friendly `AKS cluster '...' is Stopped. Start it first.` message —
  unchanged.
* Footer line shows the last auto-stop (or last skip reason) when one
  exists: `Last auto-stop 2026-05-29 14:00 (idle:60m)` /
  `Last skip 2026-05-29 13:55 (active_jobs:2)`.

## API / IaC diff summary

### New backend modules
* [api/services/auto_stop.py](../../../api/services/auto_stop.py) —
  `AutoStopPreference` dataclass + Azure-Table / file-fallback storage
  mirroring `auto_warmup`. Public helpers:
  `get_auto_stop_preference`, `save_auto_stop_preference`,
  `list_auto_stop_preferences`, `extend_auto_stop_preference`,
  `mark_auto_stop_event`, `is_extended`, `is_in_cooldown`.
* [api/services/auto_stop_evaluator.py](../../../api/services/auto_stop_evaluator.py) —
  Pure `evaluate_cluster(pref, *, repo, now=None, power_state="")` →
  `IdleDecision(verdict, reason, next_stop_at, seconds_until_stop,
  active_job_count, cluster_power_state)`. Verdicts: `stop` / `warn` /
  `keep`. Decision gates (short-circuit order): `disabled` →
  `power_state ≠ Running` → `cooldown` (30 min) → `extended` →
  `active_jobs > 0` → `state_repo_unreachable` → idle deadline. Active
  job types: `blast / warmup / prepare_db / shard / oracle`.
* [api/tasks/azure/idle_autostop.py](../../../api/tasks/azure/idle_autostop.py) —
  Two Celery tasks:
  * `evaluate_idle_clusters` (beat) walks every persisted preference and
    enqueues `auto_stop_aks` for clusters whose evaluator returns
    `verdict="stop"`.
  * `auto_stop_aks` re-runs the evaluator **inside the task body**
    before calling `stop_aks.run(...)` — this closes the
    decide-vs-act race window so a BLAST submitted between beat tick
    and worker pick-up aborts the auto-stop.

### New backend routes
All four mounted under `/api/aks/*` via
[api/routes/aks/autostop.py](../../../api/routes/aks/autostop.py):

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/aks/autostop` | Read the current preference (returns the default off shape when no row exists). |
| PUT  | `/api/aks/autostop` | Upsert `{enabled, idle_minutes}` for `(sub, rg, cluster)`. |
| POST | `/api/aks/autostop/extend` | Push the next-stop deadline out by `minutes` (default 30). |
| GET  | `/api/aks/autostop/status` | Live evaluator verdict for the SPA banner — never 500s, ARM unreachable degrades to `verdict="keep"`. |

### Beat schedule
[api/celery_app.py](../../../api/celery_app.py) gains `aks-idle-autostop-evaluate`:
`api.tasks.azure.evaluate_idle_clusters`, every 300 s by default
(override via `CELERY_BEAT_AKS_IDLE_AUTOSTOP_SECONDS`).

### Frontend
* [web/src/api/aks.ts](../../../web/src/api/aks.ts) — new
  `aksApi.autoStop.{get,save,extend,status}` methods +
  `AutoStopPreferenceResponse` / `AutoStopStatusResponse` interfaces.
* [web/src/components/ClusterItem/AutoStopPanel.tsx](../../../web/src/components/ClusterItem/AutoStopPanel.tsx) —
  Toggle + dropdown + countdown banner + Extend button. Uses TanStack
  Query (`useQuery` for both preference and status, `useMutation` for
  save + extend). Status poll is **briskly 60 s** while running +
  enabled, **5 min** while disabled, **off** while cluster is not
  running.
* [web/src/components/ClusterItem/ClusterItem.tsx](../../../web/src/components/ClusterItem/ClusterItem.tsx) —
  Renders the panel inside the existing `expansionExtras` block; the
  outer guard now opens whenever the cluster is operational so the
  panel is reachable even on clusters with no databases.

### IaC
No Bicep changes — the new feature uses the existing platform Storage
account (Tables) and existing Celery sidecars; no new identity, no new
permissions.

## Charter compliance notes

* **Storage `publicNetworkAccess: Disabled` invariant — unchanged.** The
  evaluator reads `jobstate` rows via the existing `state_repo` (Table
  endpoint, same MI), the autostop preference table is identical to
  `autowarmup` and goes through the same private endpoint.
* **No SAS to browser — unchanged.** The new routes return JSON only.
* **MI bearer / `DefaultAzureCredential` for ARM — unchanged.** The
  auto-stop task body calls the existing `api.tasks.azure.stop_aks`
  which already uses the shared MI.
* **No new Azure Functions / Durable Functions / VM** — pure Celery
  beat + worker work, fits the bundled Container App layout.
* **Cooldown safety net.** A cluster that just auto-stopped is left
  alone for 30 minutes so a researcher who immediately restarts is
  never kicked back into a stop loop.
* **Re-check inside the task body.** The driver evaluates twice (once
  in beat, once in the worker task body) so a racing BLAST submit
  cannot lose to a stale beat decision.

## Validation evidence

* Backend: `uv run pytest -q api/tests/test_auto_stop.py
  api/tests/test_auto_stop_evaluator.py
  api/tests/test_aks_autostop_route.py
  api/tests/test_auto_stop_task.py` → **44 passed** (post-hardening; 34
  pre-hardening).
* Backend wide sweep:
  `uv run pytest -q api/tests` (parallel, default markers) → 1851
  passed (single unrelated `test_terminal_exec.py` flake under load,
  green when re-run in isolation).
* Backend lint:
  `uv run ruff check api/services/auto_stop*.py api/tasks/azure/idle_autostop.py
  api/routes/aks/autostop.py api/tests/test_auto_stop*.py
  api/tests/test_aks_autostop_route.py` → clean.
* Frontend tests: `cd web && npm test -- --run` → **415 passed**.
* Frontend build: `cd web && npm run build` → ✓ clean.

## Self-critique hardening (2026-05-29 follow-up)

The first cut shipped a working cost-saver but a critical review surfaced
10 issues. Eight were fixed in the same change set:

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | HIGH | `verdict="stop"` emitted even when ARM `power_state` was unknown — could double-stop a deleting / mid-provision cluster | Evaluator now requires `power_state == "Running"` for `stop`; unknown returns `keep / power_state_unknown` |
| 2 | HIGH | `auto_stop_aks` had `autoretry_for=(Exception,) max_retries=2`; combined with `stop_aks`'s 3 retries → up to 9 ARM calls per beat tick | Removed autoretry on `auto_stop_aks`; the next beat tick re-evaluates fresh |
| 3 | HIGH | Beat-overlap race — slow tick could enqueue a second `auto_stop_aks` for the same cluster | Beat now stamps `last_stop_at` BEFORE enqueueing, so the second tick's `is_in_cooldown` gate refuses |
| 4 | HIGH | `_count_active_jobs` issued 5 identical `list_for_scope` Table queries (no server-side `type` filter) + `_latest_activity_ts` issued a 6th | Merged into one `_scan_cluster_jobs()` query per tick; new regression test `test_active_jobs_count_uses_single_query` |
| 5 | HIGH | `/api/aks/autostop/extend` accepted up to 24 h — single misclick disables cost-saver for a workday | Pydantic field capped at `MAX_EXTEND_MINUTES = 4 * 60`; route returns 422 on excess |
| 6 | MEDIUM | `/api/aks/autostop/status` uncached — N clusters × M browsers polling every 60 s = hot loop | 30 s in-memory per-cluster status cache + explicit `_invalidate_status_cache` on PUT / extend |
| 7 | MEDIUM | `mark_auto_stop_event` wrote on every warn tick (every 5 min during the warn window) | Only write on `warn` transition (when `last_skip_reason` does not already start with `warn:`); same de-duplication for non-warn skip reasons |
| 8 | MEDIUM | `_clamp_idle_minutes` silently rewrote invalid input (17 → 15) — unclear contract | PUT now returns 400 with allowed-set message; SPA dropdown already restricts to allowed buckets |
| 9 | MEDIUM | `_pref_response()` leaked `owner_oid` / `tenant_id` to the browser | Response shape projected through `_PUBLIC_PREF_FIELDS` allow-list |
| 10 | MEDIUM | No ownership check — any authenticated caller could mutate any cluster's preference | `_check_ownership` rejects PUT/extend when the existing row carries a non-empty `owner_oid` that does not match the caller (dev-bypass exempt). Existing `extend_until` / `cooldown_minutes` / `last_*` fields are preserved across PUT updates so a re-toggle does not reset cooldown |

Issue 10 is consistent with the rest of the dashboard (e.g.
`/api/warmup/auto-preference` follows the same first-writer-wins
pattern); full cluster-RG RBAC is out of scope for the cost-saver
shipment and tracked separately.
