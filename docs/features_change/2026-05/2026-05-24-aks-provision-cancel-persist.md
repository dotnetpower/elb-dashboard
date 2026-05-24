# AKS Provisioning UX — P3 (Cancel, Persistence, Portal Fallback)

## Motivation

Three deferred items from the [P0+P1+P2 overhaul](./2026-05-24-aks-provision-ux-overhaul.md):

1. **No cancel path.** Once `provision_aks` was enqueued, the user had to wait
   for ARM to either succeed or reject (~70 s minimum, up to 10 minutes for a
   real create) even after realizing the wrong region/SKU was picked. There
   was no way to stop the Celery task short of forcing a worker restart.
2. **Failures disappeared on reload.** The dashboard's structured error card
   only existed in React state. A browser refresh wiped it, leaving the user
   on a clean dashboard with no indication that the last provisioning attempt
   had failed — and no way to retry with the same context.
3. **Portal deep-links were fragile.** The quota action linked to
   `portal.azure.com/.../Microsoft_Azure_Capacity/QuotaMenuBlade`. Azure has
   renamed that blade more than once historically; a future rename would
   silently break the primary quota action. Only the quota category had a
   secondary docs link.

## User-facing change

### 1. Cancel in-flight provisioning
- New `Stop` chip rendered in two places while `provStatus === "creating"`:
  - Inside the modal's live provisioning panel
  - On the dashboard `ProvisioningBanner`
- Click → confirm dialog → revokes the Celery task with `terminate=True`.
  The worker honors the SIGTERM at the next ARM poll yield (≤ 20 s); the
  banner shows "Cancellation requested. Waiting for the worker to stop…"
  during the gap. The existing FAILURE/REVOKED branch in the poller then
  transitions the banner into the standard error card with
  *"Provisioning task was cancelled before it finished."*.
- The Azure cluster may already be mid-create on the ARM side; the
  confirm dialog warns the user they may still need to delete a partial
  cluster manually. We deliberately do not auto-delete from the cancel
  route — calling `managed_clusters.begin_delete` against an in-flight
  LRO races with the create completion.

### 2. "Last attempt failed" sticky banner (24 h)
- When a provision task ends in FAILURE the dashboard saves
  `{raw, clusterName, region, resourceGroup, subscriptionId, when}` to
  `localStorage` under `elb_last_failed_provision_v1`.
- On every dashboard mount we read this slot back; if it exists and is
  < 24 h old we render the same `ProvisionErrorCard` with **Edit & retry**
  and **Dismiss** buttons. The card classifier converts the raw text
  into the same friendly headline + portal deep link the live error
  card uses.
- **Edit & retry** hydrates the form (cluster name, region, RG) from
  the saved slot, then opens the modal — the user does not lose
  context on reload.
- The slot is automatically cleared when:
  - the user clicks Dismiss,
  - a subsequent provision succeeds (the existing "done" transition
    calls `clearLastFailedProvision()`),
  - the entry is older than 24 h (pruned on read),
  - the stored shape is malformed (defensive against schema drift).
- REVOKED tasks (cancelled by the user) are **not** saved — a
  deliberate cancel does not deserve a sticky reminder on reload.

### 3. Portal deep-link hardening
- Quota portal link switched from the canonical
  `portal.azure.com/#blade/Microsoft_Azure_Capacity/QuotaMenuBlade/myQuotas`
  to the durable `aka.ms/quotas/view-quotas` shortlink. The aka.ms forward
  is owned by the Azure capacity team and survives blade renames; the
  full URL is kept as a secondary docs action so the user has two paths.
- Every classifier category (`quota`, `sku_blocked`, `region`,
  `rg_permission`, `auth`) now has at least one docs action as a
  fallback for the deep-link.

## API / IaC diff

### Backend
- **NEW** [`api/routes/aks/cancel.py`](../../../api/routes/aks/cancel.py)
  — `POST /api/aks/cancel-provision/{task_id}`:
  - Verifies ownership via the same `JobStateRepository.find_by_task_id`
    + `owner_oid` check the `/api/tasks/{id}` read route uses, so the
    cancel route can't be a softer authorization than the read.
  - `AsyncResult.status` is read first; terminal states return
    `{was_running: false}` without calling revoke (idempotent).
  - Running tasks: `celery_app.control.revoke(task_id, terminate=True,
    signal="SIGTERM")` + `update_state(job_id, "cancelled_by_user",
    status="cancelled", error_code="cancelled_by_user")`.
  - Response carries `settle_after_seconds` so the FE knows the
    worker may take up to one ARM poll interval (~20 s) to honor the
    signal.
- Wired into `api/routes/aks/__init__.py` above the existing provision
  route include.
- **NEW** [`api/tests/test_aks_cancel_provision.py`](../../../api/tests/test_aks_cancel_provision.py)
  — 4 tests: revoke + state update, idempotent on terminal states,
  rejects non-owner with 403, passes through when no state row exists.

### Frontend
- **NEW** [`web/src/components/cards/ClusterCard/lastFailedProvision.ts`](../../../web/src/components/cards/ClusterCard/lastFailedProvision.ts)
  + `.test.ts` (4 vitest cases) — localStorage helper with 24 h
  freshness window, malformed-entry pruning, in-process MemoryStorage
  shim for the test environment.
- `web/src/api/aks.ts`: added `AksCancelProvisionResponse` type and
  `aksApi.cancelProvision(taskId)` typed client.
- `web/src/components/cards/ClusterCard/useClusterProvisioning.ts`:
  - new `cancelProvision()` action that POSTs to the cancel route,
    optimistically sets a "Cancellation requested…" message until
    the poller lands the canonical REVOKED.
  - failure handling now calls `saveLastFailedProvision(...)` (only on
    FAILURE, not REVOKED).
  - success handling now calls `clearLastFailedProvision()`.
  - new `applyLastFailedContext({clusterName, region, resourceGroup})`
    so the dashboard "Last attempt failed" banner can repopulate the
    modal on Edit & retry.
- `web/src/components/cards/ClusterCard/ProvisionModal.tsx`:
  - new `onCancel?` prop; renders a Stop chip next to the live
    progress header, wrapped in a confirm dialog so the user
    acknowledges the partial-cluster caveat.
- `web/src/components/cards/ClusterCard/ProvisioningBanner.tsx`:
  - new `onCancel?` prop; renders a `Stop provisioning` chip next
    to the existing portal link in the banner footer (also wrapped
    in a confirm dialog).
- `web/src/components/cards/ClusterCard/ClusterCard.tsx`:
  - hydrates `lastFailed` from localStorage on mount.
  - renders the "Last attempt failed" `ProvisionErrorCard` when the
    modal is closed and no live `provError` is already showing.
  - threads `cancelProvision` into both the modal and the banner.
- `web/src/components/cards/ClusterCard/armErrorClassifier.ts`:
  - `portalQuotaUrl` switched to `aka.ms/quotas/view-quotas` (durable
    shortlink) with the same query-string contract the canonical
    blade accepts.
  - region / rg_permission / auth / sku_blocked categories all gain
    docs fallback actions.
- `web/src/components/cards/ClusterCard/armErrorClassifier.test.ts`:
  - updated quota tests to accept either the aka.ms shortlink or the
    canonical portal URL (both forward to the same blade).

No infra / Bicep / Celery task body changes. The provision_aks task
itself is unchanged.

## Validation

- `uv run pytest -q api/tests/test_aks_cancel_provision.py
  api/tests/test_aks_availability.py api/tests/test_azure_provision_aks.py
  api/tests/test_azure_tasks.py` — **23 passed** (4 new + 19 existing).
- `uv run ruff check api/routes/aks/ api/services/aks_availability.py
  api/tests/test_aks_cancel_provision.py` — All checks passed (after a
  single auto-fix for import ordering in `aks/__init__.py`).
- `cd web && npx vitest run src/components/cards/ClusterCard/` —
  **9 passed** (4 new lastFailedProvision + 5 existing classifier).
- `cd web && npm run build` — built in 6.65 s, no TypeScript errors.

Manual scenario coverage:
- **Cancel during preflight wait**: hit Create → modal stays open with
  live progress → click Stop in modal → confirm → banner shows
  "Cancellation requested…" → REVOKED status arrives ~10–20 s later →
  error card renders with the "cancelled before it finished" copy.
- **Cancel from dashboard**: same flow but with the modal closed (ESC
  or backdrop after Create) → Stop chip on the dashboard banner does
  the same thing.
- **Reload after failure**: trigger a quota failure → close browser
  tab → reopen dashboard → "Last attempt failed" card appears with
  the classifier headline and portal link → Edit & retry repopulates
  the modal with the saved region/RG/cluster name.
- **Slot clears on success**: trigger a failure, then provision
  successfully → the sticky banner disappears (cleared by the "done"
  transition in `useClusterProvisioning`).
- **Stale entry pruning**: with the helper unit tests confirming
  entries > 24 h are silently dropped on read.

## Deferred (out of scope for P3)

- **Server-side failed-task surface.** The `JobStateRepository` already
  has a more authoritative record than localStorage, but exposing it
  would require a new `/api/aks/recent-failed-provisions` route plus
  filtering by task kind. The localStorage helper covers the single-
  browser case; the server surface is for cross-browser visibility.
- **Storage-event cross-tab sync.** Currently `lastFailed` is only
  hydrated once on mount. A user with two tabs open would not see
  a failure from the other tab until they reload. Minor edge case.
- **Auto-delete partial clusters on cancel.** Calling
  `managed_clusters.begin_delete` against an in-flight create LRO is
  racy. We surface the caveat in the confirm dialog instead.
