# AKS cluster card → "Single-Line Pulse" (Variant A)

## Motivation

The per-cluster card on the dashboard had grown to seven stacked rows
(header band, bento, optional start-estimate, sharding chips, pool grid,
sharding capacity, state row, details modal trigger). It used the
full width of a 1366×768 viewport for one cluster and made it hard to
scan across multiple clusters at a glance.

Operators told us:

- they read the **name + health verdict + a few numbers** first;
- they only open node pools / network / kubelet identity occasionally;
- recently-submitted jobs are what they care about right after a submit,
  not pool-by-pool capacity.

## User-facing change

Each cluster now renders as a **single-line pulse row**:

```
[● health]  cluster-name · status-line .............  [Submits 15m] [Active] [Pressure]  ▾
```

Expanding the row reveals three stacked sections:

1. **Meta grid** (4 columns × 2 rows) — Region · K8s · Nodes · DBs ·
   CPU peak · Mem peak · API p95 · Errors 15m. Numbers are colour-tone
   by threshold (warning ≥70%, danger ≥85%).
2. **Jobs section** — up to 4 jobs sorted Pending → Running →
   Reducing → Failed → Completed, newest first within a state. Each
   line shows: state pill, query (mono), per-DB chip, splits progress
   bar, ETA / elapsed time, submitter. A "+N more jobs" link opens
   the existing cluster detail modal.
3. **Action row** — Start / Stop / Open cluster detail / Delete.

Stopped, transitioning, and provisioning clusters default to **open** so
the Start button is immediately visible; healthy running clusters default
to **collapsed**; degraded running clusters default to open so the
"why" is visible without a click. Open/closed state persists per-cluster
in `localStorage` under `elb-cluster-pulse-collapsed-<name>`.

Auto-warmup, sharding chips, and the start-estimate panel remain visible
inside the expansion. The full node-pool / network / identity detail
modal is still available via the **Open cluster detail** button (it is
the same modal the old "compact node summary" strip used to open).

## File diff summary

New (one folder, eight focused modules, none above ~250 lines):

- [web/src/components/cards/ClusterPulse/ClusterPulse.tsx](../../../web/src/components/cards/ClusterPulse/ClusterPulse.tsx)
  — shell: collapse state + lifecycle predicates + orchestration only.
- [web/src/components/cards/ClusterPulse/usePulseSignals.ts](../../../web/src/components/cards/ClusterPulse/usePulseSignals.ts)
  — collects `useNodeSummary`, `blastApi.listJobs`,
    `monitoringApi.requestMetrics` and returns view-ready aggregates
    (active count, submits/15m, peak user-pool CPU/mem, p95, errors).
- [web/src/components/cards/ClusterPulse/useClusterHealth.ts](../../../web/src/components/cards/ClusterPulse/useClusterHealth.ts)
  — pure verdict (`HealthTone` + status line) derived from signals.
- [web/src/components/cards/ClusterPulse/PulseRowSummary.tsx](../../../web/src/components/cards/ClusterPulse/PulseRowSummary.tsx)
  — collapsed header button (HealthDot + name + 3 stats + chevron).
- [web/src/components/cards/ClusterPulse/PulseMetaGrid.tsx](../../../web/src/components/cards/ClusterPulse/PulseMetaGrid.tsx)
  — 4×2 meta grid.
- [web/src/components/cards/ClusterPulse/JobsSection.tsx](../../../web/src/components/cards/ClusterPulse/JobsSection.tsx)
  — jobs roster + a single shared 1-second tick (one `setInterval`
    instead of one-per-row).
- [web/src/components/cards/ClusterPulse/JobLine.tsx](../../../web/src/components/cards/ClusterPulse/JobLine.tsx)
  — one job row; receives `nowMs` from JobsSection instead of owning a timer.
- [web/src/components/cards/ClusterPulse/PulseActions.tsx](../../../web/src/components/cards/ClusterPulse/PulseActions.tsx)
  — Start / Stop / Open detail / Delete buttons.
- [web/src/components/cards/ClusterPulse/atoms.tsx](../../../web/src/components/cards/ClusterPulse/atoms.tsx)
  — HealthDot, PulseStat, MetaCell, ActionBtn, DbChip, JobStatePill
    (Pending now uses the `Clock` icon, not `Server`).
- [web/src/components/cards/ClusterPulse/helpers.ts](../../../web/src/components/cards/ClusterPulse/helpers.ts)
  — `fmtMs`, `fmtSec`, `jobStateTone`, `jobTimeText`, `noteToneFor`,
    `ownerLabel` (renders the local part of an UPN when present).
- [web/src/components/cards/ClusterPulse/index.ts](../../../web/src/components/cards/ClusterPulse/index.ts) — re-exports `ClusterPulse`.
- [web/src/pages/mockups/AksCardMockupsSimple.tsx](../../../web/src/pages/mockups/AksCardMockupsSimple.tsx)
  — three variants (design reference, wired at `/mockups/aks-card-simple`).

Hardening pass on top of the initial SRP split:

- a11y — header button has `aria-expanded` + `aria-label`; progress bar
  has `role="progressbar"` + `aria-valuemin/max/now/label`; every
  decorative icon is `aria-hidden`.
- perf — JobsSection owns the only 1-second timer; it stops when no
  rendered job is in an active state.
- correctness — `dbCounts.warming` removed (the `useClusterDbChips`
  output was "ready", not "warming"); meta cell now reads
  `N visible[·M infeasible]`.
- types — dropped `as { degraded?: boolean }` cast for request metrics
  (the field is on `RequestMetricsSummary`), and removed the unused
  `useNodeSummary` re-export from the shell.

Modified:

- [web/src/components/ClusterItem/ClusterItem.tsx](../../../web/src/components/ClusterItem/ClusterItem.tsx)
  — drops imports of `ClusterBento`, `ClusterHeaderBand`,
    `ClusterStateRow`, `PoolCardsGrid`, `ShardingCapacityRow`,
    `useClusterActiveSubmissions`. Renders `<ClusterPulse>` with
    `expansionExtras` carrying the sharding chip strip + start-estimate
    panel, and mounts `<ClusterDetails>` in controlled mode
    (`hideTrigger`, `open`, `onOpenChange`) so the pulse's "Open
    cluster detail" button drives the existing modal.
- [web/src/components/ClusterDetailModal/ClusterDetails.tsx](../../../web/src/components/ClusterDetailModal/ClusterDetails.tsx)
  — adds optional `hideTrigger`, `open`, `onOpenChange` props.
    Existing uncontrolled callers are unaffected.
- [web/src/App.tsx](../../../web/src/App.tsx)
  — adds `/mockups/aks-card-simple` route.

Backend / IaC: no change. Data sources reused (`monitoringApi.requestMetrics`,
`blastApi.listJobs`, `useNodeSummary`, `useClusterDbChips`).

The previously inline sub-components
(`ClusterHeaderBand`, `ClusterBento`, `PoolCardsGrid`,
`ShardingCapacityRow`, `ClusterStateRow`, `useClusterActiveSubmissions`)
are still on disk and exported; nothing imports them after this change.
They will be deleted in a follow-up sweep once we are confident the
pulse covers every operational scenario.

## Validation

- `cd web && npm run build` → succeeded (1 chunk-size warning, unchanged
  from baseline).
- Local dashboard smoke at `http://127.0.0.1:18080/` confirmed the
  pulse row renders with live data for `elb-cluster`:
  - Header: name + "API p95 5.4s" status line (yellow degraded dot
    because p95 > 2s).
  - Pulse stats: Submits 15m 0 · Active 0 · Pressure 26%.
  - Meta grid populated from `useNodeSummary` (CPU 10%, Mem 26%, Nodes 11)
    and `useClusterDbChips` (5 ready · 2 warming).
  - Jobs section: 4 most recent jobs, each with the correct sub-state
    line (kubectl apply error, warning, etc.).
  - Sharding chip strip and Stop / Open cluster detail / Delete buttons
    rendered below.

## Critique-hardening pass (follow-up, same day)

A second pass walked every visible affordance with the same critical
eye operators apply when something goes wrong. 20 issues found, all
fixed in this PR; bucketed below.

### Correctness — job classification & counters

1. `classifyJobState` now treats a row with a non-empty `error` as
   **Failed** even when `phase`/`status` are missing or unrecognised.
   Previously these rows became "Unknown", which silently zeroed
   `failed/15m` and the row-level border tone.
   ([web/src/components/cards/ClusterBento/jobMapping.ts](../../../web/src/components/cards/ClusterBento/jobMapping.ts))
2. `toJobRowView` forwards `j.error` into the classifier, so the same
   correction applies inside the pulse roster.
3. `usePulseSignals` now also returns `unknownCount` and re-uses the
   `{ error }` aware classifier when computing `failed15m` /
   `completedToday`.
   ([web/src/components/cards/ClusterPulse/usePulseSignals.ts](../../../web/src/components/cards/ClusterPulse/usePulseSignals.ts))
4. `JobsSection` header surfaces a `N unknown` chip whenever the
   classifier truly cannot bucket a row — so users no longer see
   `0 active · 0 done` while four rows sit in the roster.

### Readability — JobLine

5. New helper `prettifyQueryLabel` strips the
   `queries/uploads/<uuid>/` storage prefix so the table shows
   `query.fa` instead of a UUID path.
   ([web/src/components/cards/ClusterPulse/helpers.ts](../../../web/src/components/cards/ClusterPulse/helpers.ts))
6. New helper `summariseNote` drops the redundant `ERROR:` prefix,
   collapses whitespace and clamps to ~80 chars (full text remains in
   the row `title`).
7. New helper `estimateEtaSec` derives a remaining-time estimate from
   `splits_done / splits_total / elapsed` when the backend has not
   pushed an explicit ETA, so Running rows show
   `1m12s · ETA 4m30s` instead of just elapsed.
8. JobLine renders a small `#<id8>` mono chip with the full job id on
   hover, so users can correlate a row with a CLI log line at a
   glance.
9. JobLine row is keyboard-activatable (`role="link"`, `tabIndex=0`,
   Enter/Space + click) and navigates to `/blast/jobs/{job_id}` — the
   row was visually clickable before but had no action.
   ([web/src/components/cards/ClusterPulse/JobLine.tsx](../../../web/src/components/cards/ClusterPulse/JobLine.tsx))
10. JobLine grows a 3-px tone border-left coloured by state
    (Failed=danger, Completed=success, Running=accent, Reducing=teal,
    Unknown=warning), turning the roster into a glance-table.
11. Submitter falls back to `—` (with hover "Submitter not recorded")
    instead of "user" when `owner_upn` is missing — the previous label
    made every job look like the same submitter.

### Layout — Pulse actions reachable

12. `PulseActions` moved to the **top** of the expanded panel and
    given a bottom border, so Start / Stop / Open detail / Delete are
    reachable without scrolling past a long Jobs roster.
    ([web/src/components/cards/ClusterPulse/ClusterPulse.tsx](../../../web/src/components/cards/ClusterPulse/ClusterPulse.tsx))
13. Each PulseAction button wears a `title` attribute explaining what
    it actually does (e.g. "Stop the AKS cluster — paused billing, no
    running jobs", "Delete the cluster and its node pools
    (irreversible)").
    ([web/src/components/cards/ClusterPulse/PulseActions.tsx](../../../web/src/components/cards/ClusterPulse/PulseActions.tsx))

### Discoverability — tooltips on every stat

14. `PulseStat` and `MetaCell` both accept an optional `tooltip` prop
    that renders a `title` and switches the cursor to `help`.
    ([web/src/components/cards/ClusterPulse/atoms.tsx](../../../web/src/components/cards/ClusterPulse/atoms.tsx))
15. PulseRowSummary attaches tooltips to **Submits 15m**, **Active**,
    **Pressure** so the unit and the source are unambiguous (no more
    guessing whether Pressure means CPU or memory).
    ([web/src/components/cards/ClusterPulse/PulseRowSummary.tsx](../../../web/src/components/cards/ClusterPulse/PulseRowSummary.tsx))
16. PulseMetaGrid attaches tooltips to **all eight** meta cells.
    ([web/src/components/cards/ClusterPulse/PulseMetaGrid.tsx](../../../web/src/components/cards/ClusterPulse/PulseMetaGrid.tsx))
17. The cluster name and status line each get their own `title` for
    full-text hover (they ellipsis on narrow viewports).

### Accessibility

18. PulseRowSummary wires `aria-controls={panelId}` to the expanded
    panel id (allocated via `useId` in ClusterPulse).
19. JobLine's progressbar already had `role/aria-value*`; the row
    itself now carries an `aria-label` (`Open job <id> (<state>).
    <query>.`).

### Navigation

20. JobsSection's `+N more jobs` button now navigates to
    `/blast/jobs?cluster=<name>` instead of opening the per-cluster
    detail modal (which never showed jobs).
    ([web/src/components/cards/ClusterPulse/JobsSection.tsx](../../../web/src/components/cards/ClusterPulse/JobsSection.tsx))
21. JobsSection's empty state "No jobs yet" turns "submit one" into a
    clickable link to `/blast/submit`.

### Validation of the hardening pass

- `cd web && npm run build` → `✓ built in 12.16s`, no new warnings.
- Browser re-verified at `http://127.0.0.1:18080/`:
  - Stop / Open cluster detail / Delete now sit immediately under the
    pulse row (no scroll required).
  - Empty Jobs section shows `No jobs yet — submit one` with the
    link styled in `--accent`.
  - DBs cell reads `5 visible`, Errors 15m = 0 (neutral tone).
  - Pulse stat hover text reads e.g. "Higher of CPU peak and Mem peak
    across user-pool nodes" on Pressure.
