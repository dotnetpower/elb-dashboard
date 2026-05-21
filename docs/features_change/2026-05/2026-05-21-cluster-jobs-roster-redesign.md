# Cluster Pulse — Jobs roster visual hierarchy pass

## Motivation

The cluster card's jobs roster had three readability problems:

1. **Identity was a single long line** — `20260521-1046 MPXV F3L - NC_003310.1`
   ran across the row with the program / db / query rendered as plain
   middle-dot text underneath, so it was unclear which part was the ID and
   which were facets.
2. **The failure note inlined with the meta line** — for the FAILED row,
   `node_warmup_not_ready` sat next to `query.fa` and read like metadata
   rather than an error.
3. **The status word was low-contrast** — `COMPLETED` / `FAILED` were
   rendered at ~10% opacity background which competed poorly with the
   left-edge colour bar.
4. **The USER column was always present** even when no row had an owner
   (`—` in every cell), wasting 76 px of the title column.

## User-facing change

* `JobLine` now renders identity as a **stacked two-row block**: title on top,
  three explicit chips (`program` accent / `db` strong / `query` mono) below.
  The bullet / spinner shares a fixed 12 px gutter so the two lines align
  with the row's colour bar.
* The job note is no longer part of the identity meta. When a row carries
  a note it renders as a **full-width stripe** beneath the row cells,
  tinted with a danger / warning / info palette derived from
  `noteSeverity`, with a small `AlertTriangle` icon. FAILED rows always
  use the danger tint so `node_warmup_not_ready` is visually owned by the
  failure.
* The status pill is now **filled** (75% mix of the phase colour with a
  35% outline) so COMPLETED / FAILED / RUNNING reads from across the card.
* The `User` column is **auto-hidden** when no visible row has an
  `owner_upn`. Both `JobsTableHeader` and `JobLine` switch grid templates
  from `1fr 76px 76px 92px` to `1fr 76px 92px`.

## Backend / IaC diff

None. UI-only change.

## Files

* [web/src/components/cards/ClusterPulse/JobLine.tsx](../../../web/src/components/cards/ClusterPulse/JobLine.tsx)
  — replaced `BlastJobIdentity` usage with an inline title + chip row,
  added `ChipMono`, the `noteSeverity` helper, the filled status pill,
  and the failure stripe row.
* [web/src/components/cards/ClusterPulse/JobsSection.tsx](../../../web/src/components/cards/ClusterPulse/JobsSection.tsx)
  — derives `hasOwners` from `jobIndex`, threads it to header + rows.
* [web/src/theme/dashboard-layout.css](../../../web/src/theme/dashboard-layout.css)
  — rewrote the mobile `.pulse-job-row > *:nth-child(N)` selectors to
  use the new class names (`.pulse-job-identity`, `.pulse-job-status-cell`,
  `.pulse-job-timeblock`) so the responsive collapse still works after
  the User column became conditional and the note stripe became a 5th
  child.

`BlastJobIdentity` is untouched; the other two call sites
(`ClusterBento/atoms.tsx`, `cards/JobCard.tsx`) keep their behaviour.

## Validation

* `cd web && npm run build` — clean, 13.34 s.
* Manual screenshot of `http://127.0.0.1:8090/` against the live
  `elb-cluster` showing 2 COMPLETED rows + 1 FAILED row with the
  `node_warmup_not_ready` stripe rendered beneath the FAILED row, and
  the header collapsed to `JOB / STATUS / TIME` (no User column).
