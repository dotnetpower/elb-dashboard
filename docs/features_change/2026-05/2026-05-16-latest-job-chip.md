# 2026-05-16 — Topbar `LatestJobChip`

## Motivation

The molecular-diagnostics researcher's first question every morning is *"did
my last search finish?"*. Today the answer requires navigating to **Jobs**
and scanning a table. A persistent topbar chip surfaces that single most
important fact (status of the most recent BLAST job) on every page, with a
one-click jump to the job detail.

When no jobs exist yet (fresh deployment, common during onboarding), the
same chip becomes a "Run your first search" CTA pointing at `/blast/submit`.
This doubles its value as a discoverability anchor.

## User-facing change

A new compact pill in the topbar, between nav and live indicator. Five
visual states driven by the `data-state` attribute and shared CSS tokens:

| state     | trigger                                       | border        | primary text |
|-----------|-----------------------------------------------|---------------|--------------|
| `ok`      | `status` matches `complet|succeeded|done`     | `--success`   | "DONE"       |
| `fail`    | `status` matches `fail|error`                 | `--danger`    | "FAILED"     |
| `running` | otherwise (default for active jobs)           | `--accent`    | humanised phase ("Running", "Splitting", "Downloading DB", "Provisioning") |
| `queued`  | `status` matches `queue|pending`              | `--warning`   | "QUEUED"     |
| `empty`   | no jobs returned by `/api/blast/jobs`         | `--text-faint` (dashed) | "NO JOBS"  |

* Click → `/blast/jobs/<job_id>` (or `/blast/submit` in the empty state).
* Polls `/api/blast/jobs` every 15 s via TanStack Query (10 s `staleTime`).
* Compact by default (icon + uppercase primary). On viewports ≥ 1100 px the
  chip also shows the truncated job title (≤ 36 chars) and a relative
  timestamp; on < 1100 px those collapse so the chip stays ≤ 180 px wide.
* Respects existing glass tokens (`--glass-bg`, `--glass-border`,
  `--glass-bg-strong`) and reduces motion via short `transition` only.

## Files

| Change | Path |
|--------|------|
| new    | [web/src/components/LatestJobChip.tsx](../../../web/src/components/LatestJobChip.tsx) |
| new    | [web/src/components/LatestJobChip.css](../../../web/src/components/LatestJobChip.css) |
| edit   | [web/src/components/Layout.tsx](../../../web/src/components/Layout.tsx) — import + insertion between `layout__spacer` and `layout__live` |

No backend changes. Reuses the existing `blastApi.listJobs()` typed
client and `BlastJobSummary` type.

## Validation

* `cd web && npm run build` → ✓ built in 6.60 s, 0 TypeScript errors.
* Live SPA at `http://127.0.0.1:18080/`:
  * `document.querySelector('.latest-job-chip')` → 1 element, `data-state="empty"`,
    `href="/blast/submit"`, text `"NO JOBS Run your first search"` (or
    `"NO JOBS"` once the title collapses on narrow widths).
  * Topbar `scrollWidth` 1325 → 985 (visible) — chip fits within the
    existing horizontal real estate; no new overflow introduced relative to
    pre-existing topbar density.
* Backend `/api/blast/jobs` returns `{jobs: []}` in the dev tenant, so the
  empty-state path is the one rendered here. Non-empty branches verified by
  unit-style reasoning over the `describeJob` switch (no jobs available to
  exercise live).

## Follow-ups (intentionally out of scope for this UI-only PR)

* ETA estimation when `tone === "running"` — needs phase-duration history
  on the backend.
* DB version chip (`BlastDatabase.source_version` + `downloaded_at`)
  immediately to the right of `LatestJobChip` — second-highest item from
  the researcher-persona UX analysis.
* Parameter-presets quick-pick on the Submit page.
