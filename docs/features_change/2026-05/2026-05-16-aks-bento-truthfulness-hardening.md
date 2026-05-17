# 2026-05-16 — AKS bento truthfulness + noise hardening

## Motivation

Visual + code review of the production AKS bento card on a live cluster
turned up 20+ "the card lies / the card is noisy" findings ([previous
session in this conversation]):

* `Healthy` badge was rendered while `API p95 = 2352 ms` (red) — the
  health classifier branched over `power_state`, `cpuPct`, `memPct`,
  `apiErrors`, `failed15m` but **not** `p95`.
* "Submit pipeline · 15m" headline read `0 submits` while the sparkline
  underneath drew a peak — the spark was actually `metrics.rpm` (i.e.
  every `/api/blast/*` HTTP request, including dashboard polls) and
  not the submit timeline.
* "CPU 1% / Memory 1%" because the cluster-wide average dilutes a
  hot user-pool node against four idle system nodes.
* Live activity rail filled with eleven near-identical
  `[RemovingNode] node/aks-blastp16v3-vmss00000N` rows (one per node),
  all with green check icons, all timestamped "56s ago".
* Topology said `NODES 3 · POOLS 2` while the events showed ten vmss
  ordinals — confusing scope.
* Active jobs and Recent runtime cells both surfaced
  `job state store unavailable` from the same root cause.

User asked to play critic and implement every cycle ("모든단계 진행하고
비평 하드닝").

## User-facing change

### Truthfulness fixes

| # | Before | After |
|---|--------|-------|
| 1 | `Healthy` while `API p95 = 2352 ms` | `Degraded` once `p95 > 2000 ms` (configurable `P95_DEGRADED_MS`). |
| 2 | Hero sparkline = `/api/blast/*` request RPM, mislabelled as "submit pipeline". | Hero sparkline is now a **per-minute submit timeline** built from `clusterJobs.created_at`. Annotated with `peak: N/min` and `Submits per minute · last 60m`. The original RPM signal moved to the Pulse strip as a dedicated `API RPM · peak/min` KPI. |
| 3 | `CPU 1% / Memory 1%` (cluster-wide average). | `CPU peak X% · avg Y%` and `Mem peak X% · avg Y%`, where peak is the most-loaded **user pool** node from `/api/monitor/k8s/top-nodes`. Avg shown as the small grey hint. |
| 4 | `NODES 3 · POOLS 2` (configured count only). | `NODES N ready · user M` from live `nodeSummary`, with a `M not-ready` hint when applicable. `POOLS system X · user Y` derived from `agent_pools[].mode`. |

### Noise / information density fixes

| # | Before | After |
|---|--------|-------|
| 5 | Eleven identical `[RemovingNode]` rows per scaledown event. | `groupEvents()` collapses events sharing `(reason, involved_kind)` within a 90-second window into one row. Names with a long shared prefix render as `aks-blastp16v3-vmss000000..009 (10)`. Up to 12 grouped rows visible; a quiet `+N older events not shown` footer surfaces overflow. |
| 6 | `RemovingNode`, `NodeNotSchedulable`, `Drain`, `Cordon`, scaling activity all rendered with a green check (Normal-type). | New `EventKind = "info"` with a muted-blue `Info` icon, gated by an `INFO_NOTABLE_REASONS` set. K8s still calls these Normal, but the operator no longer reads them as "all good". |
| 7 | Event lines capped at 90 chars, truncating vmss ordinals. | Cap raised to 140 chars, and grouping moves the ordinal into the leading `kind/name` chunk so it is never the part that gets cut. |
| 8 | Live Activity rail had no namespace cue. | Non-`default` and non-`kube-system` namespaces are surfaced as `ns/<name>` so BLAST job churn is distinguishable from kubelet noise. |
| 9 | Active Jobs cell showed `Active jobs · —` (dash inside the eyebrow) when the job store was degraded; Recent Runtime cell repeated the same `job state unavailable` hint. | Active jobs eyebrow drops the dash entirely when degraded; Recent Runtime cell collapses to a single muted `—` so the hint is not duplicated. |
| 10 | "Live Activity" header had no scope/count. | `30 events` label in the rail header (raw event count from the `/api/monitor/aks/events` payload). |

### Polish

| # | Before | After |
|---|--------|-------|
| 11 | `Open` button (no tooltip). | `Show details` button with `title="Show pool, node, and per-database detail"`. |
| 12 | `0 / 1h · 0 / 24h` — slash readable as fraction. | `1h: N · 24h: M` — colon makes the relationship explicit. |
| 13 | Hero `0` rendered as a hostile bare zero when the cluster was idle. | New `EmptySubmitState` row: friendly empty card with a `Run a search` CTA wired to `onOpenDetail`. |
| 14 | API p95 KPI had no SLA reference. | KPI hint reads `ms · SLA 2000` and a `PressureBar` underneath fills against the SLA so the operator can see the headroom at a glance. |

### New tests

`web/src/components/cards/ClusterBento/eventMapping.test.ts` — 13
vitest cases locking down classification (`info` vs `ok`, `warn` vs
`err`) and grouping behaviour (vmss collapse, namespace prefix,
distinct reasons stay separate, malformed timestamps don't crash, the
single-event branch keeps the original message).

## API / IaC diff

None — pure SPA presentation change. No backend route, schema, Bicep,
or Celery task touched. The card consumes the same
`/api/monitor/aks/events`, `/api/monitor/metrics`,
`/api/monitor/k8s/top-nodes`, and `/api/blast/jobs` endpoints as
before.

## Files touched

* `web/src/components/cards/ClusterBento/ClusterBento.tsx` — health
  classifier (+`p95` branch), hero submit timeline + empty state, peak
  CPU/Mem from user-pool nodes, topology live nodes + pool mode split,
  Active Jobs / Recent Runtime degraded dedupe, `Show details` label,
  `1h: · 24h:` formatting, `API RPM` KPI, sparkline peak label.
* `web/src/components/cards/ClusterBento/atoms.tsx` — `EventKind` adds
  `"info"` (muted-blue `Info` icon).
* `web/src/components/cards/ClusterBento/eventMapping.ts` — rewritten
  with `groupEvents()`, `INFO_NOTABLE_REASONS`, namespace prefix,
  message-cap aware grouping, and a back-compat `toEventLineView()`.
* `web/src/components/cards/ClusterBento/eventMapping.test.ts` —
  **NEW**, 13 vitest cases.

## Validation

* `cd web && npx vitest run` → **41 passed** (incl. 13 new
  `eventMapping.test.ts`).
* `cd web && npm run build` → tsc + Vite bundle clean (`✓ built in
  5.47s`, no warnings beyond the pre-existing `chunkSizeWarningLimit`).
* `uv run pytest -q api/tests` → **411 passed** (no regression — no
  backend change).
* Live browser check at <http://127.0.0.1:18080/>:
  * Header pill is `Healthy` while `p95 = 22 ms`; once p95 drifted to
    `1790 ms` the pill stayed Healthy (still `<2000`) but the
    `PressureBar` filled to ~89% — the operator now has a visual
    "approaching SLA" cue. A subsequent run with `p95 = 2352 ms` (the
    case that originally triggered the review) would render
    `Degraded` per the new branch in
    `web/src/components/cards/ClusterBento/ClusterBento.tsx`.
  * `Live activity` rail shows blue **info** icons for `RemovingNode`,
    `NodeNotSchedulable`, `Drain`, `Cordon` — no more green checks
    masquerading as health signals — and the `30 events` count
    reflects the raw payload size.
  * `CPU peak 13% · avg 2%` and `Mem peak 18% · avg 2%` — peak
    surfaces user-pool pressure that the prior cluster average hid at
    `1%`.
  * `Topology` shows `NODES 4 ready · user 3` and `POOLS system 1 ·
    user 1` instead of the old `NODES 3 · POOLS 2`.
  * `Active jobs` eyebrow has no trailing `· —`; `Recent runtime · 24h`
    renders a single muted `—` instead of a duplicate degraded hint.
  * Hero CTA reads `Show details`; subtotals read `1h: 0 · 24h: 0`.

## Known follow-ups (intentionally out of scope here)

* **#19 Trash-icon label** — lives in `ClusterItem.tsx`, not the bento
  itself. Belongs in a separate header/affordance pass.
* **#20 Refresh indicator tooltip** — also outside the bento subtree
  (`Dashboard` page footer/timer atom).
* **#16 K8s patch version** — backend currently surfaces
  `kubernetes_version` (e.g. `1.34`) but not
  `current_kubernetes_version` (e.g. `1.34.5`). A separate change to
  `api/services/monitoring.py` plus the `AksClusterSummary` shape is
  needed before the bento can render the patch.
* **DATABASES section header** — sits below the bento; the visual
  orphan is a `ClusterItem.tsx` concern.
