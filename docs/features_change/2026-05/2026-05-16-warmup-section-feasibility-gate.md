# 2026-05-16 — WarmupSection consumes warmup_plan (Phase 1 follow-up #1)

## Motivation

Phase 1 (`2026-05-16-warmup-feasibility-planner.md`) shipped the
backend planner and surfaced its verdict on the AKS cluster card's
chip strip. The "Start warmup" CTA inside the cluster detail modal
(`WarmupSection.tsx`) still let users trigger a warmup the planner
already knew would fail. This follow-up wires the same `warmup_plan`
data into the modal so the select option / Warmup button reflect
feasibility before the user clicks.

`ComputeSection.tsx` (BLAST submit page) is intentionally **not**
touched in this change — it consumes the same `BlastDatabase` shape
but is currently entangled with sharding logic (`dbSharded`,
`dbShardSets`, `dbTotalBytes`) that is being reworked in another
session ("정밀샤딩"). Adding plan-driven gating there will be a
separate PR after that work lands, to avoid merge conflicts and
double-thinking the sharding contract.

## User-facing change

Inside the AKS cluster detail modal → DB Warmup section:

- The DB select dropdown now appends a hint to any database whose
  warmup is infeasible on the current cluster topology, e.g.
  `Core nucleotide (~250 GB) — too large for current cluster (add
  nodes or upgrade SKU)`. Such options are also `disabled` so they
  cannot be picked.
- The Warmup button is disabled when the currently selected DB has
  `feasible=false`, with a tooltip showing the planner message.
- Below the select, when the chosen DB has any non-`ok` plan status,
  a coloured advisory renders the message + every recommendation:
  - red ("Warmup blocked") for infeasible verdicts.
  - amber ("Warmup advisory") for `ok_unknown_sku` (informational —
    the action is still allowed because the planner is using the
    fallback 64 GiB RAM heuristic).
- Defence-in-depth: `handleStartWarmup` re-checks feasibility and
  refuses with `setStartError(...)` if a keyboard / programmatic
  activation slips through the disabled button.

## API / IaC diff summary

### Frontend only

- `web/src/components/WarmupSection.tsx`
  - `downloadedQuery` cache key now includes `nodeCount` and
    `nodeSku` (key prefix `blast-databases-warmup`), and
    `listDatabases` is called with the cluster topology so the
    backend attaches `warmup_plan`.
  - New `planByName: Map<string, BlastWarmupPlan>` index for O(1)
    lookup; `selectedPlan` and `selectedInfeasible` derived state.
  - Select `<option>` and Warmup `<button>` consume that state for
    `disabled` and `title` attributes.
  - New advisory block (`role="alert"` for infeasible / `role="note"`
    for advisory) renders message + recommendations.
  - `handleStartWarmup` short-circuits with a `startError` when
    `selectedInfeasible` is true.

No backend, infra, or test surface changes. The Phase 1 backend
already returns `warmup_plan` whenever cluster topology is supplied.

## Validation evidence

```
$ cd web && npm run build
✓ built in 4.41s
dist/assets/index-C442UjBr.js   671.97 kB │ gzip: 183.17 kB
```

```
$ uv run pytest -q api/tests
(unchanged from Phase 1 — no backend code touched in this follow-up)
```

Live SPA: cluster detail modal opens; the DB Warmup select renders
the candidates list; backend `/api/blast/databases?...&num_nodes=...&
machine_type=...` returns `warmup_plan` per row (verified earlier).
The advisory renders only when a DB is actually selected, so the
section keeps its previous "empty" appearance until the user picks a
candidate.

## Hardening review

- **No new dependencies.** Pure additions to an existing component.
- **Cache key change is non-breaking.** The previous cache entry
  (no topology suffix) was only consumed inside this component;
  nothing else keys off `["blast-databases-warmup", ...]`. Other
  pages (Dashboard `ClusterItem`, BLAST Submit) keep their own
  cache namespaces.
- **No SAS, no ARM round trip added.** Plan computation is server-
  side and already covered by Phase 1 hardening.
- **XSS safe.** All planner strings render through React's text
  interpolation or the `title` attribute (browser does not parse
  `title` as HTML).
- **Defence in depth.** Disabled button is paired with a guard in
  `handleStartWarmup`. A future malicious / scripted caller would
  also be rejected by the backend orchestrator's own preflight, so
  this is purely a UX guard.

## Follow-ups (unchanged from Phase 1)

- **Phase 2** — actual vmtouch DaemonSet (Celery task), gated by a
  server-side preflight that re-runs the planner.
- **Phase 3** — per-DB × stage matrix view on the AKS card.
- **ComputeSection** — wire the same plan signal into the BLAST
  submit page once the precision-sharding rework in the sibling
  session lands. The plan should at minimum render an inline warning
  when the user picks a DB whose warmup would refuse on the cluster
  they selected; whether it should hard-block submit is a product
  decision (the BLAST job will still run, just much slower).
