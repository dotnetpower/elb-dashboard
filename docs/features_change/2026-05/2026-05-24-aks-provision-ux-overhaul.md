# AKS Provisioning UX Overhaul — P0+P1+P2 (preflight rigour, modal lifecycle, error card, live preflight, smart defaults)

## Motivation

Previous flow (still on the dashboard until this change):

1. User picks defaults (`Standard_E16s_v5` × 10 + `Standard_D2s_v3` × 1
   = 162 vCPUs) in `koreacentral` where their subscription has 100
   regional vCPU quota. Pre-flight ran and surfaced a yellow
   `Quota may be insufficient` row — but classified the shortfall as
   `warn`, **not blocking submit**.
2. User clicked Create → modal closed immediately on `aksApi.provision()`
   acceptance → dashboard banner showed "Provisioning…" for ~1 m 10 s.
3. ARM rejected with `ErrCode_InsufficientVCPUQuota: ... left regional
   vcpu quota 100, requested quota 162`.
4. The dashboard rendered a raw red wall of text (full Azure SDK error,
   verbatim, with duplicated `Code:`/`Message:` tail) above the cluster
   list. Form values from the modal were gone — to retry the user had
   to re-open the modal and re-enter everything.

Three categories of fault combined to produce this UX:

- **A.** Pre-flight semantics were too lenient (quota = warn).
- **B.** Modal lifecycle closed before ARM validation finished.
- **C.** Error rendering was raw and offered no actionable next step.

## User-facing change

The Create AKS Cluster flow is now:

1. **Live pre-flight** — the moment the modal opens (and on every form
   edit, debounced 500 ms), the SPA calls `/api/aks/preflight` and
   renders the result inline. Pre-flight is **not** triggered by the
   cluster-name field (name has no effect on SKU/quota/RG checks).
2. **Smart default** — if the very first pre-flight result returns a
   quota failure that has a workable fit (e.g. needs 162 vCPUs, can
   only fit 6 nodes of the chosen SKU), the modal auto-applies that
   `max_blast_nodes_fit` to the Node Count input *as long as the user
   has not manually touched it*. The result is the next pre-flight
   passes without a click.
3. **Quota = fail (blocking)** — once the user *does* edit the form
   into an infeasible state, the Compute quota row turns red and the
   `Create Cluster` button reads `Fix errors above` and refuses submit.
   The row includes an inline `Apply N nodes` button (or a "no node
   count fits" hint when the SKU alone exceeds the headroom).
4. **Live cores total** — under the SKU description, the modal now
   reads `16 cores, 128 GB RAM, E-v5 memory · 10 × 16 = 160 cores
   total` so the user can see at a glance which knob to turn.
5. **Modal stays open through ARM validation** — clicking Create no
   longer closes the modal. A compact "Provisioning · Step 3/5 ·
   arm_create_or_update · 0m 45s" panel renders in the footer until
   either:
   - the task publishes `cluster_state` for the first time (ARM
     accepted), at which point the modal auto-closes and the dashboard
     banner takes over; or
   - the task fails — the modal stays open with all form values
     intact and renders the new error card.
   Closing the modal manually (ESC or backdrop) during this window
   shows a confirm dialog so an accidental key press doesn't yank the
   live progress out from under the user.
6. **Structured error card** — replaces the raw red text. The card
   carries:
   - title + classifier-driven one-line summary
     (`Quota too small in koreacentral — needs 162 vCPUs, you have
     100 free.`)
   - category-specific secondary message
   - action buttons (Request quota increase ↗ that deep-links to the
     subscription/region quota blade, SKU/region docs links, Edit &
     retry, Dismiss)
   - raw Azure response folded into a `<details>` accordion for
     debugging only
   - The same card is used inside the modal and on the dashboard
     (when the user closed the modal before the error landed).

## API / IaC diff

### Backend
- [`api/services/aks_availability.py`](../../../api/services/aks_availability.py)
  - `run_provision_preflight`: quota shortfall now emits
    `status="fail"` (was `warn`). The row's `details` carry
    `max_blast_nodes_fit` (largest blast node count that fits under
    the binding family/total cap, accounting for system pool overhead)
    plus `blast_cores_per_node`, `system_cores_total`, `binding_family`
    so the FE can render an "Apply N nodes" button.
  - `overall_ok` is now derived from rendered rows (`any status=="fail"`)
    so future additions of `fail` paths can't drift out of sync with
    the manual flag.
- No new routes; the existing `/api/aks/preflight` payload picks up
  the richer `details` automatically.
- [`api/tests/test_aks_availability.py`](../../../api/tests/test_aks_availability.py)
  adds `test_run_provision_preflight_fails_when_quota_short` which
  asserts the new contract (`status="fail"`, `binding_family="Total
  Regional vCPUs"`, `max_blast_nodes_fit=6` for the canonical
  reproduction scenario).

### Frontend
New / changed files under `web/src/components/cards/ClusterCard/`:

- **NEW** `armErrorClassifier.ts` + `.test.ts` — regex-based ARM
  error classifier returning `{category, summary, details, actions[]}`.
  Categories: `quota`, `sku_blocked`, `region`, `rg_permission`,
  `auth`, `unknown`. The classifier:
  - extracts requested-vs-free numbers from quota messages,
  - strips the "Provisioning task failed:" wrapper our poller adds,
  - drops the duplicated `Code:`/`Message:` tail Azure emits,
  - deep-links to the subscription + region scoped quota blade
    (`portal.azure.com/.../QuotaMenuBlade/myQuotas`) instead of the
    generic docs URL.
  5 vitest cases lock in the regex contracts.
- **NEW** `ProvisionErrorCard.tsx` — reusable structured failure card
  used in both the modal (when the task fails) and the dashboard
  (when the user closes the modal before the error lands).
- **MODIFIED** `useClusterProvisioning.ts`:
  - removed the `closeModal()` call from `handleProvision`; modal
    close is now driven from a `useEffect` that watches
    `taskProgress.cluster_state` (or the cluster appearing in the
    list).
  - added live debounced pre-flight (500 ms, modal-open gated,
    silently swallows failures so the Create-flow pre-flight remains
    the authoritative path).
  - added smart-default effect that applies `max_blast_nodes_fit`
    on the first failing live pre-flight while
    `nodeCountUserTouched === false`.
  - added `resetError()` (clears error + invalidates cached pre-flight
    so the next Create runs fresh).
  - added `modalOpen` prop so the live pre-flight effect doesn't
    run when the modal is closed.
- **MODIFIED** `ProvisionModal.tsx`:
  - new props: `taskPhase`, `taskProgress`, `elapsed`,
    `subscriptionId`, `onErrorReset`.
  - new compact "Live provisioning" panel rendered while
    `provStatus === "creating"`.
  - error rendering replaced with `<ProvisionErrorCard>`.
  - quota fail row in the preflight check list gains the
    `Apply N nodes` button (and a "no node count fits" hint when
    `max_blast_nodes_fit === 0`).
  - SKU description gains the running cores total
    (`10 × 16 = 160 cores total`).
  - ESC + backdrop dismiss both require a confirm while
    `provStatus === "creating"`.
- **MODIFIED** `ClusterCard.tsx`:
  - passes the new props through.
  - the dashboard `provError` slot now renders the same
    `ProvisionErrorCard` (only when the modal is closed — we don't
    want both surfaces to render the same error).

No infra / Bicep / Celery task changes. No new dependencies.

## Validation

- `uv run pytest -q api/tests/test_aks_availability.py
  api/tests/test_azure_provision_aks.py api/tests/test_azure_tasks.py`
  — 19 passed.
- `uv run ruff check api/services/aks_availability.py
  api/routes/aks/preflight.py api/tasks/azure/provision.py
  api/tests/test_aks_availability.py` — All checks passed.
- `cd web && npx vitest run
  src/components/cards/ClusterCard/armErrorClassifier.test.ts` —
  5 passed.
- `cd web && npm run build` — built in 5.78 s, no TypeScript errors.
- Manual scenario (the exact reproduction from the user report):
  - Open Create modal on a subscription with 100 regional vCPU quota
    in koreacentral → the Node Count auto-snaps from 10 to 6 within
    ~1 s as the live preflight runs and the smart default applies.
  - Click Create → modal stays open, live progress panel ticks
    through `creating_cluster` → `ensuring_resource_group` →
    `arm_create_or_update` → as soon as ARM publishes
    `cluster_state="Creating"` the modal auto-closes and the
    dashboard banner takes over.
  - If the user instead manually picks `10` (touching nodeCount) and
    clicks Create, the form rejects submit with a structured "Quota
    too small" card carrying an `Apply 6 nodes` button.

## Deferred (P3 — not in this change)

Documented for the next pass:

- **Cancel provisioning** (`/api/aks/cancel-provision/{task_id}` +
  Celery `revoke()` + partial-resource cleanup).
- **Failed-provision persistence** across browser reloads (the
  JobStateRepository already has the row; the dashboard just doesn't
  surface "last failed task in 24 h" on first paint).
- **Quota deep-link blade name verification** — the classifier links
  `Microsoft_Azure_Capacity/QuotaMenuBlade/myQuotas` which is the
  current portal blade; Azure occasionally renames blades. If a
  future Azure portal rev makes the link 404, fall back to
  `https://aka.ms/quotas/view-quotas`.
