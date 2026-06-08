# 2026-06-08 — Live E2E validation of node scaling + warmup; two fixes

## Motivation

Live end-to-end validation of the new workload-node scaling feature on the
deployed dashboard: start `elb-cluster-02`, scale its `blastpool` from 10 to 5
nodes, confirm the warmup re-runs, and confirm a BLAST search runs. The
validation surfaced two defects that this change fixes, plus one pre-existing
unrelated infrastructure issue (reported, not fixed here).

## What was validated live (deployed revision 286 / commit 9cad296)

| Step | Result |
| --- | --- |
| Start `elb-cluster-02` via `POST /api/aks/start` | OK (~4.5 min to Running/Succeeded) |
| Scale `blastpool` 10→5 via `POST /api/aks/scale` (+ `auto_warmup`) | OK (~1 min; ARM `provisioningState=Succeeded`) |
| Kubernetes node check | OK — exactly 5 `blastpool` nodes `Ready` (+1 systempool) |
| Warmup machinery (explicit `POST /api/warmup/start` 16S) | OK — 5 `warm-16s-*` jobs, all `SuccessCriteriaMet` in ~30 s |
| Automatic re-warm chained by the scale | **GAP** — no warmup jobs created (BUG2 below) |
| BLAST submit (inline 16S) | **Blocked** by a pre-existing openapi config bug (BUG3 below) |
| Stop `elb-cluster-02` via `POST /api/aks/stop` | OK — cost halted |

## Fixes in this change

### BUG1 — `scale_aks` lifecycle timing was dropped
`scale_aks` recorded an `aks_scale` lifecycle timing, but `aks_scale` was not a
known phase in `api/services/cluster_timings.py`, so every scale logged
`cluster_timings: refusing to record unknown phase 'aks_scale'` and the duration
was discarded. Registered `aks_scale` (default 90 s) so the timing persists.

### BUG2 — SPA auto-sync silently cleared a pending forced re-warm
`scale_aks` (and `start_aks`) persist `force_rewarm_pending=true` on the
auto-warmup preference so the recurring beat reconcile keeps forcing a re-warm
until the cluster is workload-ready. But the SPA's `ClusterItem` auto-sync
fires `PUT /api/warmup/auto-preference` whenever the database list **or the live
node count** changes — including immediately after a scale, when
`cluster.node_count` flips 10→5. That PUT is an unconditional "user wins"
upsert whose body does not carry `force_rewarm_pending`, so it reset the flag to
`false` and the forced re-warm was silently dropped — leaving the freshly
(re)scaled nodes cold. Confirmed live: the post-scale preference showed a
different user's `owner_oid`, `databases:[core_nt]` (their localStorage default),
`num_nodes:5`, and `force_rewarm_pending:false`.

Fix: `warmup_auto_preference_put` now carries `force_rewarm_pending` forward from
the persisted row when the incoming body omits it (an explicit
`force_rewarm_pending:false` still wins, so the reconcile's own bookkeeping path
can clear it). This hardens both the `scale_aks` and the pre-existing
`start_aks` re-warm chains.

## Pre-existing issue surfaced (NOT fixed here)

### BUG3 — `elb-openapi` deployed with an empty `ELB_STORAGE_ACCOUNT`
The inline BLAST submit failed with a 503: the sibling `elb-openapi` pod ran
`azcopy cp … https://.blob.core.windows.net/queries/…` — an **empty storage
account host** — and timed out after 60 s. The pod's `ELB_STORAGE_ACCOUNT` env
var is present but empty (`name` only, no `value`). The manifest builder
(`api/tasks/openapi/manifests.py`) is correct (`value=storage_account`), so this
specific live deployment was created with `storage_account=""`. This is an
openapi-deploy concern, independent of node scaling — tracked separately; the
remedy is to redeploy elb-openapi with a non-empty storage account.

## API / IaC diff summary

- `api/services/cluster_timings.py`: add `aks_scale` to `DEFAULT_SECONDS`.
- `api/routes/warmup.py`: `warmup_auto_preference_put` preserves a pending
  `force_rewarm_pending` from the existing row unless the body sets it explicitly.
- Tests: `test_cluster_timings.py::test_aks_scale_phase_is_known`;
  `test_warmup_route.py::test_auto_preference_put_preserves_pending_force_rewarm`
  and `::test_auto_preference_put_honours_explicit_force_clear`.
- No IaC changes.

## Validation evidence

- `uv run ruff check api` → All checks passed.
- `uv run pytest -q api/tests` → 3118 passed, 3 skipped.
- Live: scale 10→5 confirmed at ARM + Kubernetes level; explicit 16S warmup
  completed on all 5 nodes; cluster stopped afterwards.

## Remaining risk / follow-up

- BUG2's fix addresses the confirmed clobbering root cause. A follow-up live
  cycle should re-verify that the scale auto-chain now produces warmup jobs
  end-to-end (this run proved the warmup machinery works via the explicit path
  but could not re-verify the auto-chain after the fix without another paid
  cluster start).
- BUG3 (empty `ELB_STORAGE_ACCOUNT`) blocks OpenAPI-plane BLAST submits on this
  cluster until elb-openapi is redeployed with a valid storage account.
