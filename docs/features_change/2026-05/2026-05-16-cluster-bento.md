# Cluster Bento — port mockup to production with graceful degrade

## Motivation

The dashboard's AKS cluster card has historically been a stack of
infrastructure-detail rows (pool grid, sharding ceiling, DB chips,
state row, modal opener). That answers "what is provisioned" but not
the questions an operator actually asks first when they open the
dashboard:

- *Are submits flowing through right now?* (last 15m / 1h / 24h, trend
  vs the prior 15m window)
- *Is the API healthy?* (p95, error rate over the same window)
- *Is the cluster under load?* (CPU / Memory pressure aggregate)
- *Which jobs are currently in flight, and how far have they shaded?*
- *What is the cluster talking about right now?* (Kubernetes Events)

The P3 "Mission Control Bento" mockup answered those five questions in
one tight 3-column / 4-row grid. This change ports that mockup into
production code so the live dashboard renders the same layout against
real backend data.

## User-facing change

When an AKS cluster's body is expanded, the first thing the operator
sees is now a 7-cell bento:

1. **Submit pipeline · 15m** (hero) — 15m submit count + 1h/24h
   secondaries, trend badge vs prior 15m, health pill, "Open" CTA, and
   a 60-bucket sparkline of submit RPM.
2. **Live activity** (rail) — newest Kubernetes events for the
   cluster, plain background, monospace prefixes for verb/payload
   tokens, relative timestamps.
3. **Pulse** (strip) — `API p95` / `Errors 15m` / `CPU` / `Memory`
   inline KPIs.
4. **Active jobs** — up to 4 active submissions with split-progress
   strip and "+N more" overflow link.
5. **Topology** — Nodes / SKU / Pools / K8s.
6. **Recent runtime · 24h** — finished count + still-running count.

Every cell renders the "—" placeholder + a one-line degraded hint when
its upstream is unavailable (e.g. "job state store unavailable" when
Storage Table is offline locally). The bento itself never breaks the
card.

The legacy "deep technical" rows (PoolCardsGrid, ShardingCapacityRow,
ClusterStateRow, the modal-opening ClusterDetails) are now collapsed
behind the bento's "Open" button (state persisted per cluster in
`localStorage`). The DatabaseChipStrip (sharding actions) stays
visible at all times because it is action-bearing, not informational.

## API / IaC diff summary

**Backend (`api/`)**

- **NEW**
  [api/services/request_metrics.py](../../api/services/request_metrics.py)
  — process-local request metrics ring buffer (deque + `threading.Lock`,
  capacity from `REQUEST_METRICS_CAPACITY`, default 8192). Path
  normalisation collapses path-params for `/api/blast/jobs/{id}` etc.
  so the cardinality stays bounded. Nearest-rank percentile, error
  count includes status >= 500 and the dispatch-failure sentinel 0.
- **MODIFIED** [api/main.py](../../api/main.py) — the existing
  `RequestIdMiddleware.dispatch` now records into the metrics buffer
  on both success and exception paths. Skips `/api/health`,
  `/api/monitor/sidecars/*`, `/api/monitor/metrics`, and non-`/api/*`
  so the metrics endpoint cannot pollute itself.
- **NEW route** `GET /api/monitor/metrics` in
  [api/routes/monitor.py](../../api/routes/monitor.py) — returns
  `{window_seconds, count, rpm, p50_ms, p95_ms, p99_ms, error_count,
  error_rate, rpm_buckets[]}`. Auth-gated via `Depends(require_caller)`,
  `path_prefix` validated to start with `/api/` (400 otherwise).
- **NEW route** `GET /api/monitor/aks/events` in
  [api/routes/monitor.py](../../api/routes/monitor.py) — proxies
  `k8s_list_events`, namespace validated against DNS-1123 regex
  `^[a-z0-9-]{1,64}$`, message sanitised via `sanitise()` and capped
  at 512 chars.
- **NEW helper** `k8s_list_events` in
  [api/services/k8s_monitoring.py](../../api/services/k8s_monitoring.py)
  — direct K8s API (no AKS Run Command), sorted newest-first, returns
  flat dicts `{namespace, name, type, reason, message, count,
  last_timestamp, involved_kind, involved_name, source_component,
  source_host}`. Re-exported by
  [api/services/monitoring.py](../../api/services/monitoring.py).
- **MODIFIED** [api/routes/stubs.py](../../api/routes/stubs.py) —
  `_local_to_blast_job()` now adds `query_label` (from payload
  `query_file` / `query_name` / `queries`, capped 120 chars) and
  derived `splits_done` / `splits_failed` / `splits_total` from
  `split_children.children_by_status`. Case-insensitive matching for
  the completed / failed buckets.

**Frontend (`web/`)**

- **NEW** [web/src/components/cards/ClusterBento/](../../web/src/components/cards/ClusterBento/)
  module: `atoms.tsx` (HealthPill, TrendBadge, Spark, NumberDisplay,
  PressureBar, KpiInline, JobStateBadge, SplitProgress, JobRow,
  EventLine, BentoCell), `jobMapping.ts` (classify job state into
  `pending|running|reducing|completed|failed`, build `JobRowView`),
  `eventMapping.ts` (classify event severity, relative timestamps,
  shorten message at 110 chars), `ClusterBento.tsx` (the 7-cell
  layout), `index.ts` (barrel).
- **MODIFIED**
  [web/src/components/ClusterItem/ClusterItem.tsx](../../web/src/components/ClusterItem/ClusterItem.tsx)
  — renders `<ClusterBento>` at the top of the expanded body; the
  legacy deep-detail rows are gated on a new per-cluster
  `detailsExpanded` state toggled by the bento's "Open" CTA and
  persisted via `localStorage`.
- **MODIFIED** [web/src/api/monitoring.ts](../../web/src/api/monitoring.ts)
  — typed clients `monitoringApi.requestMetrics(...)` and
  `monitoringApi.aksEvents(...)`, plus `RequestMetricsSummary` /
  `K8sEvent` interfaces.
- **MODIFIED** [web/src/api/blast.ts](../../web/src/api/blast.ts) —
  `BlastJobSummary` gains optional `splits_done` / `splits_failed` /
  `splits_total` / `query_label`.

**IaC** — no changes. Endpoints reuse the existing `api` sidecar and
managed identity; no new Bicep modules.

## Security / hardening notes (reviewed in this PR)

- All new routes use `Depends(require_caller)` and therefore reject
  unauthenticated callers with 401 (with the existing
  `AUTH_DEV_BYPASS=true` local-dev escape hatch).
- `path_prefix` on `/api/monitor/metrics` is validated to start with
  `/api/` to block reflection-style probes (e.g. `path_prefix=/`).
- `namespace` on `/api/monitor/aks/events` is validated against the
  DNS-1123 regex `^[a-z0-9-]{1,64}$` *before* the K8s request goes
  out.  `resource_group` and `cluster_name` are validated against
  Azure's `^[A-Za-z0-9._\-()]{1,90}$` so a path-traversal-shaped value
  (`../etc`) is rejected with HTTP 400 before any Azure SDK call.
- Event messages are passed through `sanitise()` and capped at 512
  chars before being returned to the SPA.  Defence-in-depth: the
  `k8s_list_events` helper itself caps every free-form string field
  (name 253, namespace 63, reason 64, message 1024, source_*  ≤ 253)
  and clamps `count` into `[1, 1_000_000]` so a malformed controller
  storm cannot bloat the JSON response or render eye-watering
  numbers in the UI.  `type` is coerced into the closed K8s enum
  (`Normal | Warning`) so the frontend classifier never sees
  attacker-controlled severity strings.
- The metrics buffer is bounded (`deque(maxlen=capacity)`) and
  protected by `threading.Lock`.  Path normalisation strips IDs/UUIDs
  and *also caps each stored path at `MAX_PATH_LEN=256` chars* with a
  `…` sentinel, so fuzz traffic (`/api/aaaaaaa…`) cannot bloat
  per-sample memory or the `by_path` aggregate.
- The metrics middleware wraps its `record()` call in
  `try/except Exception` so a metrics bug can never break a request.
- No SAS tokens issued, no `azure.mgmt.*` calls outside `api/services/`,
  no AKS Run Command (we use the existing direct K8s API helper).
- `ClusterBento` queries are `enabled: isRunning` with `retry: 0` and
  staleTime 20–30s so a stopped cluster or a failing endpoint does not
  spin up polling traffic.
- Every bento cell renders a "—" + one-line degraded hint when its
  upstream is unavailable; no exceptions bubble to the cluster card.
- The `Spark` SVG component filters out non-finite samples
  (NaN/Infinity from upstream payloads where `count` came back as
  `null` or a string), so a single bad bucket cannot poison the
  rendered path with `NaN` coordinates.

## Validation evidence

- `uv run pytest -q api/tests` → **411 passed in 29.08s** (was 402 →
  +9 new hardening tests covering long-path capping in
  `normalise_path`, every k8s event field cap, count clamping, type
  enum coercion, non-dict item skip, RG / cluster_name 400s).
- `uv run ruff check api/services/request_metrics.py
  api/tests/test_request_metrics.py
  api/tests/test_local_to_blast_job.py` → **All checks passed!**
- `cd web && npx tsc --noEmit` → no errors (verified via
  `get_errors` on the touched files).
- `cd web && npm run build` → clean Vite build (671.97 kB JS gz 183.17
  kB, 105.15 kB CSS gz 33.31 kB).
- Live attack-surface smoke against `http://127.0.0.1:18080/`:
  - `GET /api/monitor/metrics?path_prefix=/etc/passwd` →
    `400 path_prefix must start with /api/`
  - `GET /api/monitor/aks/events?resource_group=../etc&cluster_name=cx`
    → `400 invalid resource_group`
  - `GET /api/monitor/aks/events?resource_group=rg-x&cluster_name=cx&namespace=../etc`
    → `400 invalid namespace`
- Live browser smoke against `http://127.0.0.1:18080/` confirms the
  bento renders inside the AKS cluster card with real data: Submit
  Pipeline 15m = 0 (Healthy pill), Live Activity shows real K8s events
  (`[Failed] 8h ago`), Pulse strip shows API p95 = 144 ms / Errors
  15m = 0 / CPU 1% / Memory 1%, Active Jobs degrades to "—" with the
  `job state store unavailable` hint (expected — Storage Table is
  not reachable from the local dev environment).
