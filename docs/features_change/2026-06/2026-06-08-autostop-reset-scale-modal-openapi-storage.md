# 2026-06-08 — Auto-stop idle-clock reset on shrink; move node scaling to detail modal; openapi storage env

## Motivation

Three issues raised after the node-scaling feature shipped:

1. **Auto-stop "immediate stop" warning after lowering the idle window.** Setting
   idle auto-stop from 4h to 1h made the dashboard show a countdown / warning as
   if the cluster were about to stop immediately.
2. **Node-count scaling control placement.** The workload-node slider/input
   belongs in the cluster detail modal, not inline on the cluster row.
3. **(Infra, BUG3) `elb-openapi` deployed with an empty storage account** — the
   auto-deploy on `aks_start` could not pass `STORAGE_ACCOUNT_NAME` because the
   `worker`/`api` sidecars did not have it set, so BLAST submits failed.

## Fixes

### BUG-A — auto-stop idle clock resets when the window shrinks
`evaluate_cluster` computes `deadline = last_activity + idle_window`. Lowering
`idle_minutes` (4h → 1h) shrinks `idle_window`; if the last activity predates the
new, smaller window, the next evaluator tick fires `warn`/`stop` immediately — so
right after the user lowers the limit the cluster looks like it is about to stop.

Fix in `put_autostop` ([api/routes/aks/autostop.py](../../api/routes/aks/autostop.py)):
- Carry `created_at` and `last_started_at` forward from the existing row (the PUT
  body never includes them, so previously every toggle reset `created_at` to now
  and dropped a real `last_started_at` — a latent bug).
- On a **downward** `idle_minutes` change, stamp `last_started_at = now` so the
  new shorter window is measured from the change moment. An **upward** change only
  extends the deadline, so the anchor is preserved (no spurious reset).

This reuses the existing drift-free `last_started_at` anchor the evaluator already
folds into the idle clock (same mechanism as a cluster start).

### BUG-B — node scaling control moved to the cluster detail modal
The `ScalePanel` (workload-node slider + input) moved from the inline
`ClusterItem` expansion into the cluster **detail modal**
([DetailsModal.tsx](../../web/src/components/ClusterDetailModal/DetailsModal.tsx)),
rendered right below the node-pools table when the cluster is Running. The modal
derives the workload pool node count + SKU from `agentPools` (prefer `blastpool`,
else first User-mode pool). `ClusterItem` no longer renders the panel inline.

### BUG3 — surface STORAGE_ACCOUNT_NAME to api/worker (infra)
[infra/modules/containerAppControl.bicep](../../infra/modules/containerAppControl.bicep)
now sets `STORAGE_ACCOUNT_NAME` on the `api` and `worker` sidecars (previously
only `terminal` had it). The auto OpenAPI deploy
(`api.tasks.openapi.auto_deploy.build_auto_openapi_payload`) runs on the worker
and reads this env; without it the deployed `elb-openapi` pod got
`ELB_STORAGE_ACCOUNT=""` and every BLAST submit failed with an azcopy upload to
`https://.blob.core.windows.net` (empty host) 60s timeout. The compiled module
JSON was regenerated; `infra/main.json` was intentionally left untouched (it is
stale against unrelated already-committed RBAC Bicep changes — regenerating it
here would pull in those out-of-scope changes; that resync is a separate task).

## API / IaC diff summary

- `api/routes/aks/autostop.py`: idle-clock anchor preservation + downward-shrink reset.
- `web/src/components/ClusterDetailModal/DetailsModal.tsx`: render `ScalePanel`.
- `web/src/components/ClusterItem/ClusterItem.tsx`: remove inline `ScalePanel` + unused imports.
- `infra/modules/containerAppControl.bicep` (+ `.json`): add `STORAGE_ACCOUNT_NAME` to api/worker.

## Validation evidence

- `uv run ruff check api` → All checks passed.
- `uv run pytest -q api/tests` → 3120 passed, 3 skipped.
  - New: `test_put_autostop_shrinking_window_resets_idle_clock`,
    `test_put_autostop_raising_window_keeps_idle_clock`.
- `cd web && npx vitest run` → 740 passed. `npm run build` → clean.

## Remaining / follow-up

- `infra/main.json` is stale vs committed RBAC Bicep; needs a separate clean
  regeneration PR. BUG3's Bicep fix only takes effect on a full `azd provision`;
  for the live cluster a one-off `az containerapp update` adding
  `STORAGE_ACCOUNT_NAME` to worker/api + an elb-openapi redeploy is the immediate
  remedy for BLAST re-validation.
