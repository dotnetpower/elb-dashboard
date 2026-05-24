# AKS Provisioning UX — Remaining items (Server-side persistence, cross-tab sync, cancel-with-partial)

Follow-up to [P3](./2026-05-24-aks-provision-cancel-persist.md) addressing
the three items explicitly deferred in that change.

## Motivation

After P3 shipped, three deferred items remained:

1. **localStorage was per-browser only.** A user who provisioned from
   their laptop and reopened the dashboard from a tablet had no idea
   the previous attempt had failed — the localStorage slot lived only
   on the originating browser.
2. **No cross-tab sync.** A failure surfaced in Tab A was invisible
   in Tab B until a manual refresh.
3. **Cancel left no path to the partial cluster.** When the user
   cancelled mid-`arm_create_or_update`, the Azure cluster resource
   may have already been created. The error card said "cancelled
   before it finished" but the user had to dig through the Azure
   portal to find and delete it.

## User-facing change

### 1. Server-side "Last attempt failed" (R-1)
- The dashboard now also fetches `GET /api/aks/recent-failed-provisions?
  hours=24&limit=1` on mount and uses the returned row as the
  authoritative source for the "Last attempt failed" banner.
- localStorage stays as the local fallback (still hydrated synchronously
  so the banner appears instantly on reload, then upgraded by the
  server response when it lands).
- Backend writes a `JobState` row with `type="aks_provision"` at
  enqueue time (previously the row was missing and every state
  update silently no-op'd against the table). `task_id` is stamped
  in a second `update()` call after `_safe_delay` returns so
  ownership lookups by task id (cancel + /api/tasks/{id}) resolve.
- Cross-browser: failure on the laptop now appears on the tablet on
  next dashboard load (within 24 h).

### 2. Cross-tab sync (R-2)
- The dashboard listens to the `storage` event on
  `elb_last_failed_provision_v1`. A save (or `clear()` from a
  failed-then-succeeded tab) in any other tab propagates the new
  state into the current tab without a manual refresh.

### 3. Cancel-with-partial cluster portal link (R-3)
- When a cancel lands *after* the ARM cluster resource was already
  visible (the task had published `cluster_state` at least once), the
  error card now renders an **Open cluster in Azure portal** action
  styled in the warning accent. Hover tooltip: "The cluster create
  may have started on Azure even though the task was cancelled —
  verify in the portal and delete if needed."
- Surfaced in both the dashboard error card and the modal error card
  (which use the same `ProvisionErrorCard` component).

## API / IaC diff

### Backend
- **NEW** [`api/routes/aks/recent_failures.py`](../../../api/routes/aks/recent_failures.py)
  — `GET /api/aks/recent-failed-provisions?hours=24&limit=10`:
  - Uses `JobStateRepository.list_for_owner` (existing method) and
    filters in-process by `type=="aks_provision"` + `status=="failed"`
    + freshness window. Filters in memory instead of adding a new
    repository method to keep the blast radius small.
  - `degraded=true` on `list_for_owner` failure with empty `jobs[]`
    instead of 500.
  - Bounded at `limit ≤ 20`, freshness `≤ 168 h` (one week).
  - Pulls `region` from the payload column (the summary select drops
    it); 200-row upstream limit keeps the per-row payload cost
    bounded.
- **MODIFIED** [`api/routes/aks/provision.py`](../../../api/routes/aks/provision.py)
  — now creates a `JobState(type="aks_provision", status="queued",
  owner_oid=caller, payload={...cluster context...})` before enqueuing
  the Celery task, then `update(task_id=result.id)` after enqueue.
  Both writes are best-effort; route never 500s on state-repo
  failure.
- **MODIFIED** [`api/routes/aks/__init__.py`](../../../api/routes/aks/__init__.py)
  — registers the new `recent_failures` router (above lifecycle/
  openapi).
- **NEW** [`api/tests/test_aks_recent_failed_provisions.py`](../../../api/tests/test_aks_recent_failed_provisions.py)
  — 4 tests: filters to type+status+freshness, newest-first ordering,
  degraded payload on repo failure, `limit=1` cap.

### Frontend
- **MODIFIED** [`web/src/api/aks.ts`](../../../web/src/api/aks.ts)
  — adds `AksRecentFailedProvision`, `AksRecentFailedProvisionsResponse`
  types and `aksApi.recentFailedProvisions(hours, limit)`.
- **MODIFIED** [`web/src/components/cards/ClusterCard/ClusterCard.tsx`](../../../web/src/components/cards/ClusterCard/ClusterCard.tsx)
  — mount effect now hydrates `lastFailed` from both localStorage
  (sync, instant) and the server endpoint (async, authoritative). The
  server row wins when it is strictly newer than the local snapshot.
  A second effect listens to `window.addEventListener("storage")` for
  cross-tab sync on the same key.
- **MODIFIED** [`web/src/components/cards/ClusterCard/ProvisionErrorCard.tsx`](../../../web/src/components/cards/ClusterCard/ProvisionErrorCard.tsx)
  — new `extraPortalUrl` / `extraPortalLabel` props that render a
  warning-accented action button alongside the classifier-generated
  actions. Defaults the label to "Open cluster in Azure portal" so
  the common case (cancelled-mid-create) reads naturally.
- **MODIFIED** [`web/src/components/cards/ClusterCard/ProvisionModal.tsx`](../../../web/src/components/cards/ClusterCard/ProvisionModal.tsx)
  and [`web/src/components/cards/ClusterCard/ClusterCard.tsx`](../../../web/src/components/cards/ClusterCard/ClusterCard.tsx)
  — both wire `extraPortalUrl` from `prov.taskProgress?.portal_url`
  whenever `provError` contains `"cancelled"`. The
  `taskProgress` payload already carried the portal URL from the
  P0+P1+P2 change; we just surface it more prominently here.

No infra / Bicep / Celery task body changes.

## Validation

- `uv run pytest -q api/tests/test_aks_recent_failed_provisions.py
  api/tests/test_aks_cancel_provision.py api/tests/test_aks_availability.py
  api/tests/test_azure_provision_aks.py api/tests/test_azure_tasks.py`
  — **27 passed** (4 new + 23 existing).
- `uv run ruff check api/routes/aks/ api/services/aks_availability.py
  api/tests/test_aks_cancel_provision.py
  api/tests/test_aks_recent_failed_provisions.py` — All checks passed.
- `cd web && npx vitest run src/components/cards/ClusterCard/` —
  **9 passed** (classifier + lastFailedProvision; no new tests for
  R-2/R-3 because storage-event behaviour is verified end-to-end and
  `extraPortalUrl` is a passthrough render).
- `cd web && npm run build` — built in 6.43 s, no TypeScript errors.

Manual scenario coverage (all on a real AKS provision attempt):
- **Reload survives** (R-1): trigger a quota failure → reload tab →
  banner appears within ~1 s (instant from localStorage, refined from
  server when fetch resolves).
- **Cross-browser** (R-1): trigger failure on laptop → open dashboard
  on a different browser as the same user → banner appears (server
  source).
- **Cross-tab live update** (R-2): trigger failure in Tab A → Tab B
  shows the banner without refresh; click Dismiss in Tab A → Tab B
  banner disappears.
- **Cancel-with-partial** (R-3): hit Create with valid SKU → wait
  until `cluster_state="Creating"` becomes visible (10-30 s) → click
  Stop → error card has both the "cancelled" headline *and* a warning-
  colored **Open cluster in Azure portal** button.

## Notes on the remaining edge cases

- **Race between localStorage and server hydration**: handled by the
  freshness comparison (`if (!fromLocal || serverWhen > fromLocal.when)`).
- **Server fetch fails**: `degraded=true` returned with empty jobs;
  banner falls back to localStorage silently. No user-visible error.
- **JobState row creation fails**: provision route still enqueues
  the task; recent-failures route just won't see this row (no
  banner on reload), but the live in-browser error card path is
  unaffected.
- **`storage` event quirks**: fires only on *other* tabs, not the
  originating one — the originating tab already has the new state
  in React, so no synchronization loop.
