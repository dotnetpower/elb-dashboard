# 2026-05-16 — ComputeSection consumes warmup_plan + SRP extract `useDbWithWarmupPlan`

## Motivation

Phase 1 (`2026-05-16-warmup-feasibility-planner.md`) shipped the
backend warmup-feasibility planner and surfaced its verdict on the AKS
cluster card and the cluster-detail modal's `WarmupSection`.
The BLAST Submit page (`ComputeSection`) was deliberately deferred in
follow-up #1 because it was tightly coupled to the in-flight
precision-sharding rework on `dbSharded` / `dbShardSets` /
`dbTotalBytes`.

The precision-sharding rework has now landed in this branch. This
change closes that gap: when warmup is requested but the planner says
it cannot run on the selected cluster, the **Run BLAST** button is now
disabled, the failure reason is shown inline, and a one-click
"Disable warmup and submit anyway" escape hatch is offered.

## User-facing change

On `BLAST → New Search` → *Compute Environment* section:

- The **Performance** panel renders an inline advisory beneath the
  warmup checkbox whenever the planner emits a non-`ok` verdict for
  the selected DB / cluster pair:
  - **Red ("Warmup blocked")** — `feasible=false` *and* warmup is
    requested. Lists the planner message + every recommendation, plus
    a small "Disable warmup and submit anyway" button that toggles
    `enable_warmup` off (BLAST will still run, just without the
    pre-warmed cache).
  - **Amber ("Warmup advisory")** — `feasible=true` but the planner
    has caveats (currently only `ok_unknown_sku`), or `feasible=false`
    while warmup is *not* requested (informational).
- The submit button is now `disabled` while `warmupBlocked` is true,
  and the "Required before submitting" checklist gains a clear
  blocker entry with the planner's verbatim message.
- Defence in depth: `handleSubmit` re-checks `warmupBlocked` and
  short-circuits with a toast error if a keyboard / programmatic
  activation slips through the disabled button.

The dashboard chip strip and the cluster-detail modal `WarmupSection`
already shipped this gating in Phase 1 / follow-up #1; this change
brings the Submit page into parity.

## API / IaC diff summary

### Frontend only

- **NEW** `web/src/pages/blastSubmit/useDbWithWarmupPlan.ts` (111 LOC) —
  single-responsibility hook that owns the `/api/blast/databases`
  query, derives cluster topology, memoises the selected DB row, and
  computes `warmupBlocked`. See SRP note below.
- `web/src/pages/BlastSubmit.tsx`
  - Replaced the inline `useQuery({ queryKey: ["blast-databases", ...] })`
    + `selectedDbInfo` `useMemo` with a single call to
    `useDbWithWarmupPlan(...)`.
  - Added `warmupBlocked` to `canSubmit` + a new entry in `missing[]`
    that surfaces the planner message verbatim.
  - Added a defence-in-depth guard at the top of `handleSubmit`.
  - No change to the actual submit payload — the planner is purely a
    UX gate; the backend `submit` orchestrator already runs its own
    preflight.
- `web/src/pages/blastSubmit/ComputeSection.tsx`
  - New `warmupPlan?: BlastWarmupPlan` prop.
  - New `WarmupPlanAdvisory` component renders below the warmup
    checkbox (red `role="alert"` when blocked, amber `role="note"`
    otherwise, hidden for `ok` and degenerate statuses).
  - No change to the existing sharding preview / opt-out logic — that
    code remains owned by the precision-sharding session.

No backend, infra, or test surface changes. The Phase 1 backend
already returns `warmup_plan` whenever cluster topology is supplied
on the `GET /api/blast/databases` query string.

## SRP note

Before this change, `BlastSubmit.tsx` (633 LOC) directly owned the
DB-listing query, the topology derivation, the selected-row
memoisation, **and** the warmup-feasibility derivation, in addition to
form state, cluster query, warmup-status query, submit mutation,
pre-flight mutation, render, etc. Adding the planner gating inline
made the responsibilities even more entangled.

The new `useDbWithWarmupPlan` hook collects the database +
warmup-plan concerns into one focused module:

| Responsibility                          | Owner                          |
| --------------------------------------- | ------------------------------ |
| Cluster topology derivation             | `useDbWithWarmupPlan`          |
| `/api/blast/databases` query lifecycle  | `useDbWithWarmupPlan`          |
| Selected DB row memoisation             | `useDbWithWarmupPlan`          |
| `warmupBlocked` boolean                 | `useDbWithWarmupPlan`          |
| Form state + submit / pre-flight calls  | `BlastSubmit` (page)           |
| Warmup-status (kubelet) polling         | `BlastSubmit` (different API)  |
| Sharding preview                        | `ComputeSection`               |
| Inline planner advisory                 | `ComputeSection`               |

The hook is intentionally render-free (returns plain values). The
parent page composes it with the form state and is the single source
of truth for `canSubmit`. `ComputeSection` reads `warmupPlan` and
renders advisories — it never decides whether submit is allowed.

## Validation evidence

```
$ cd web && npm run build
✓ built in 7.11s
dist/assets/index-DB1mOO45.js   671.97 kB │ gzip: 183.17 kB

$ uv run pytest -q api/tests
373 passed in 21.42s
```

(Backend untouched, but pytest is run as a regression-floor sanity.)

The precision-sharding session's reorganisation of `BlastSubmit.tsx`
(committed locally only) was briefly clobbered by an errant
`git checkout` during this task and restored from VS Code's local
history. The restored file matched line-for-line; subsequent edits
applied cleanly with no merge artefacts.

## Hardening review (pass #2 after SRP)

- **Race window on cluster switch.** When the user picks a different
  cluster, the hook's `queryKey` changes; TanStack drops the cached
  data until refetch completes (~200–300 ms). During that window
  `selectedDbInfo` is `undefined` → `selectedDbPlan` undefined →
  `warmupBlocked=false`. This is **fail-open** and matches the
  pre-existing behaviour of `dbMissingFromStorage` (which is also
  false while loading). Acceptable; the backend orchestrator's own
  preflight is the authoritative check.
- **Topology unknown.** When `selectedCluster` is undefined or its
  workload pool has no node count / SKU, the hook does not attach
  topology and the backend skips the `warmup_plan` enrichment. We
  fail-open in that case too — the user already gets a "AKS cluster"
  blocker in `missing[]`.
- **Defence in depth.** Three independent gates: disabled submit
  button, `canSubmit` boolean (which the button reads), and the
  `handleSubmit` early return. A future caller that bypasses two of
  the three still hits the third.
- **XSS.** Every planner-emitted string (`message`,
  `recommendations[]`) renders through React text interpolation or
  the `title` attribute. React auto-escapes; `title` is not parsed
  as HTML by any browser. Backend already sanitises via
  `services/sanitise.py`.
- **No new dependencies, no new ARM round trips.** Cluster topology
  was already fetched for the cluster card; the hook just reads it
  from `selectedCluster`.
- **No SAS leakage.** `warmup_plan` carries no storage URLs.
- **Cache isolation.** `WarmupSection` (modal) keys its query as
  `["blast-databases-warmup", ...]` and `useDbWithWarmupPlan` keys
  as `["blast-databases", ...]`. Two cached responses for the same
  endpoint when topologies match — slight inefficiency, complete
  isolation. Worth it.

## Follow-ups

- Phase 2 — actual vmtouch DaemonSet (Celery task), gated by a
  server-side preflight that re-runs the planner.
- Phase 3 — per-DB × stage matrix view on the AKS card.
- Consider hoisting `warmupBlocked` into the `useDbWithWarmupPlan`
  result chain when more pages need it (currently two pages: the
  cluster modal's `WarmupSection` keeps its own logic because it
  uses a different cache namespace and has different action-button
  semantics).
