# E2E mock fixtures: faithful warm-status + task-poll + delete-confirm

## Motivation

While driving the Playwright mock E2E lanes (`ui-mock` + `mutation-mock`)
directly, two scenarios failed in a way that revealed **stale test fixtures**,
not product bugs. Running the suite end-to-end is the only thing that surfaces
these, because the unit tests mock at a lower layer.

## What was wrong (and fixed)

1. **`new-search-options-matrix` — `disable_sharding` expected `false`, got `true`.**
   The warmup-status fixtures returned `core_nt` as `status: "Ready"` but
   omitted the `sources: ["warmup"]` discriminator that the SPA's
   `useWarmupStatus` requires (added 2026-05-27) to treat a DB as explicitly
   warmed. Without it the form falls back to the non-sharded profile, so the
   default blastn submit carried `disable_sharding: true`. Fixed by adding
   `sources: ["warmup"]` to the warm `core_nt` fixture in both
   `scripts/e2e/scenarios/helpers/apiMocks.ts` and
   `scripts/e2e/fixtures/mockApi.ts`, matching the real
   `api/services/k8s/warmup_status.py` contract.

2. **`destructive-actions.mutation` — AKS delete confirm never fired.**
   The AKS delete dialog gained a type-to-confirm guard
   (`ConfirmDialog` `typeToConfirm` = the cluster name) so the "Permanently
   delete" button stays `disabled` until the operator types the cluster name.
   The scenario clicked the disabled button and timed out. Fixed by typing the
   cluster name (`aks-e2e`) before the click. **This confirmed the app is
   behaving correctly — the destructive-action safety guard works.**

3. **Missing `/api/tasks/{id}` poll stub.** After a queued AKS stop/start/delete
   the SPA polls the Celery task-status endpoint until it is ready. The mock
   fixtures had no route for it, so the poll leaked to the real backend. Added a
   `**/api/tasks/{id}` route to `scripts/e2e/fixtures/mockApi.ts` returning a
   terminal `SUCCESS` so the flows resolve deterministically.

## Result

All 14 mock E2E scenarios pass (`ui-mock` 11 + `mutation-mock` 3). No product
code changed in this note — these are test-harness fidelity fixes. The
`UpgradePage` "Check remote" feedback fix (separate change note) was also
exercised live by `destructive-actions.mutation.spec.ts:34`.

## Validation evidence

- `npx playwright test --project ui-mock --project mutation-mock` — 14 passed.
- `npx vitest run src/pages/blastSubmit src/api/upgrade.test.ts` — 200 passed.

## How to run the mock E2E lanes locally (no Azure)

```bash
# one-time
npm --prefix web run e2e:install-browsers
# start web on :8090 (dev-bypass) then run the safe mock lanes
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:all-safe
```
