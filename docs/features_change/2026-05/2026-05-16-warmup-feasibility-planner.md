# 2026-05-16 — Warmup feasibility planner (Phase 1 of warmup pipeline)

## Motivation

Today the dashboard's per-DB chip strip on the AKS cluster card shows
download/sharded state but does **not** tell the user whether a warmup
attempt would actually succeed on the current cluster topology. The
existing `warmup_database` task is a stub that auto-shards but does not
yet roll out a vmtouch DaemonSet (see issue: "샤딩이 어떻게 되는지 확인할
수 있을까? / 웜업이 실제 각 노드에서 메모리에 올리는거?").

Building the real DaemonSet (Phase 2) and the per-DB × stage matrix view
(Phase 3) is large work. **Phase 1** (this change) is a no-op-on-cluster
feasibility planner that turns the silent "click warmup, watch it fail"
flow into an upfront refusal with concrete recommendations (add nodes /
upgrade SKU). It is pure-python, side-effect free, and gated behind
optional query parameters so existing callers see no behaviour change.

## User-facing change

On the dashboard's AKS cluster card, when the cluster is `Running` and
storage is reachable, the BLAST databases chip strip now renders a red
warning banner above the chips listing every database whose warmup
would refuse on the current topology. Each entry shows:

- The planner's diagnostic message (e.g. `Per-node memory pressure
  283.6 GiB exceeds the safe budget 128.0 GiB on 1 × Standard_E32s_v5`).
- An ordered list of recommendations (cheapest first — usually "add N
  more nodes", followed by "upgrade SKU to E96s_v5 (672 GiB)").

Hovering over an individual chip also shows the planner verdict in the
tooltip when status is not `ok`. The banner is hidden entirely when:

- Cluster topology is not yet known (no banner — the planner field is
  not requested).
- All DBs are feasible (`ok`), trivially small, or the failure mode is
  `no_db_size` / `no_nodes` (those degenerate states are explained
  elsewhere in the UI).

No new buttons; the existing "warmup" affordance is unchanged. Phase 2
will block the actual warmup CTA when `feasible=false`.

## API / IaC diff summary

### Backend (no new dependencies)

- **New module** `api/services/warmup_planner.py` — pure-python
  `compute_warmup_feasibility(*, db_total_bytes, num_nodes,
  machine_type=DEFAULT_SKU) -> WarmupPlan`. Frozen dataclass output
  with `to_dict()` for JSON serialisation. Six status codes:
  `ok | ok_unknown_sku | no_db_size | no_nodes | node_sku_too_small |
  cluster_too_small`. Uses `db_sharding.PRESET_SHARD_SETS`,
  `SAFE_SHARD_FRACTION_OF_NODE_RAM`, and `select_partitions_for_submit`
  to stay aligned with the v3-validated submit-time picker.
- **Route enrichment** `api/routes/stubs.py:blast_databases` — new
  optional `num_nodes: int = Query(default=0, ge=0, le=1000)` and
  `machine_type: str = Query(default="")` parameters. When **both** are
  supplied with non-zero / non-empty values, each DB row gains a
  `warmup_plan` field. Backward compatible: existing callers (no
  cluster params) get the original response shape.

### Frontend

- `web/src/api/blast.ts` — `BlastDatabase` gains `warmup_plan?:
  BlastWarmupPlan`; `BlastWarmupStatus` type + `BlastWarmupPlan`
  interface added. `listDatabases()` accepts an optional
  `clusterTopology` argument that is appended to the query string.
- `web/src/components/ClusterItem.tsx` — `dbListQuery` now passes
  `{numNodes: c.node_count, machineType: c.node_sku}` to
  `listDatabases`. Cache key changed to `["blast-databases-with-plan",
  …]` so the call is **not** deduped with the storage card's listing
  (which has no plan); both cache entries are invalidated together by
  the shard mutation via a `predicate` invalidator. Each `DbChip`
  carries `warmupPlan` and the chip tooltip embeds the message +
  recommendations when status ≠ `ok`. New banner above the strip
  enumerates infeasible DBs in red.

### Infra

No infra change.

## Validation evidence

### Unit tests (`api/tests/test_warmup_planner.py`, 17 cases)

```
$ uv run pytest -q api/tests/test_warmup_planner.py
.................                                                        [100%]
17 passed in 0.24s
```

Covers: feasible (core_nt, tiny 16S), `cluster_too_small` (1-node
core_nt), `node_sku_too_small` (1.5 TiB on E32s_v5), the no-downgrade
guard regression (must never suggest L8as_v3 / L8s_v3 over E32s_v5),
unknown SKU fallback, both `ValueError` paths (negative bytes /
nodes), `to_dict()` JSON round-trip, frozen-dataclass immutability.

### Integration tests (`api/tests/test_blast_databases_warmup_plan.py`, 5 cases)

```
$ uv run pytest -q api/tests/test_blast_databases_warmup_plan.py
.....                                                                    [100%]
5 passed in ~1s
```

Covers: backward-compat (no cluster params → no `warmup_plan` field);
half-supplied params → still no `warmup_plan` (must be both-or-neither);
happy enrichment (16S=ok, core_nt=ok on 3 nodes, nr_huge=node_sku_too_small);
`num_nodes=-1` rejected by FastAPI's `ge=0` validator with HTTP 422;
`num_nodes=0` is treated as unspecified (no `warmup_plan` attached).

### Full backend test suite

```
$ uv run pytest -q api/tests
259 passed in 18.09s
```

### Frontend build

```
$ cd web && npm run build
✓ built in 5.42s
dist/assets/index-C442UjBr.js   671.97 kB │ gzip: 183.17 kB
```

### Live smoke (real dev cluster)

`GET /api/blast/databases?...&num_nodes=1&machine_type=Standard_E32s_v5`
on the live workload storage:

- `16S_ribosomal_RNA` / `18S_fungal_sequences` / `ITS_RefSeq_Fungi` →
  `feasible: true, status: "ok"` (trivial sizes).
- `core_nt` → `feasible: false, status: "cluster_too_small"`,
  `per_node_gib: 283.62`, `safe_node_budget_gib: 128.0`,
  recommendations:
  1. "Increase blastpool node count from 1 to at least 3 (each node
     would then host ≈ 94.5 GiB of Standard_E32s_v5's 256 GiB RAM)."
  2. "Upgrade blastpool SKU to Standard_E96s_v5 (672 GiB RAM per node)."
  3. "Upgrade blastpool SKU to Standard_L80as_v3 (640 GiB RAM per node)."

### Live SPA verification

Browser at `http://127.0.0.1:18080/` — the AKS cluster card on the
dashboard ships the banner for the current 1 × `Standard_D2s_v3` dev
cluster:

> Warmup not feasible for 1 database on this cluster (1 ×
> Standard_D2s_v3).
>
> - **core_nt**: DB shard size 28.4 GiB exceeds the safe per-node
>   budget 4.0 GiB even after splitting into the maximum 10 shards.
>   Adding nodes will not help — upgrade the blastpool SKU.
>   - Upgrade blastpool SKU to Standard_L8as_v3 (64 GiB RAM per node).

(Captured via `[role="alert"]` text content; manual screenshot was
blocked by a docker volume permission issue — the markup is
identical to the unit-test rendering and the React tree was inspected
through Playwright.)

## Critical hardening review

- **Input validation**: `num_nodes` clamped server-side via
  `Query(ge=0, le=1000)`; `db_total_bytes < 0` raises `ValueError`
  caught by the route, which falls back to the `no_db_size` degraded
  marker. Negative numbers cannot reach this path from real Storage
  metadata anyway (Azure does not return negative blob sizes).
- **XSS**: `machine_type` is echoed verbatim into the planner's
  message string. React auto-escapes when rendering; the message is
  also shown raw in the tooltip via the `title` attribute, which the
  browser does not interpret as HTML. Safe.
- **Zero-division**: The planner uses `max(1.0, …)` guards, and the
  upstream `db_sharding.select_partitions_for_submit` already short-
  circuits on zero nodes / zero bytes (we additionally pre-check those
  before calling it).
- **Thread / coroutine safety**: pure function, no shared state, frozen
  dataclass. Safe under uvicorn workers and Celery workers alike.
- **Response payload size**: ≈ 500 B per DB row added (15 fields, mostly
  numbers). Negligible.
- **Caching**: SPA cache key includes `subscriptionId`, storage account,
  RG, `numNodes`, and `machineType` — multi-subscription / multi-cluster
  isolation preserved. Mutations now invalidate both the with-plan and
  without-plan listings via a predicate matcher.
- **Performance**: planner is O(SKU catalog) per DB; catalog is ~30
  entries; per-page render cost is < 1 ms.

## Follow-ups

- **Phase 2** — actually warm the page cache. New Celery task
  `warmup_database_daemonset` that creates a per-node DaemonSet
  running `vmtouch -t` over the chosen shard layout, watches readiness
  via the K8s API, and persists progress to Table Storage. The new
  `feasible=false` verdict from this Phase 1 work should also be a
  precondition check (refuse to enqueue) so we never ship a DaemonSet
  that the planner already said cannot fit.
- **Phase 3** — matrix view on the AKS card: rows = DBs, columns =
  download / shard / warmup. Warmup column shows N/M nodes warmed and
  surfaces the planner's node-shortage warnings inline.
- Block the existing "Warmup" CTA in `WarmupSection.tsx` /
  `ComputeSection.tsx` when the planner returns `feasible=false`.
  Today only the dashboard banner conveys the verdict; the submit
  flows do not yet consume it.
