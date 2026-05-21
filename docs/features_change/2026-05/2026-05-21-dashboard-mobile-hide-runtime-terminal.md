# 2026-05-21 — Dashboard hides Terminal + Sidecar runtime on mobile

## Motivation
On viewports ≤ 760px the dashboard cards wrapped awkwardly and the Terminal
and Sidecar runtime panels were not useful at phone width (the user cannot
operate the browser terminal on a phone, and the sidecar topology panel is
informational only).

## User-facing change
- Mobile breakpoint (`max-width: 760px`):
  - The "Terminal" card inside the Resource plane is hidden.
  - The entire "Sidecar runtime" section is hidden.
  - The workspace **Settings** gear button in the dashboard hero controls
    is hidden (the Help / Getting Started button stays). Settings are
    rarely re-opened from a phone, and removing the button cleans up the
    crowded right edge of the controls row.
  - The **Getting Started** modal no longer auto-opens, and the "?" re-open
    button is hidden. The checklist is a desktop-only onboarding aid; on
    a phone it covers the whole viewport and offers no useful action. If
    the viewport resizes to ≥ 761px in the same session the checklist can
    open again.
  - The **Add Cluster** CTAs (both the compact header pill on the AKS
    card and the empty-state dashed "Provision your first cluster"
    button) are hidden. Mobile is positioned as a read-only status view;
    provisioning a cluster requires the SetupWizard / Provision modal
    which is desktop-only.
  - **Cluster pulse card** is compacted for narrow screens:
    - The Stop / Delete action buttons next to "Open cluster detail" are
      hidden. Mobile is read-only; destructive ops stay on desktop.
    - Stat labels in the summary row (Submits 15m / Active / Pressure)
      collapse to just the icon + value so the row fits.
    - The 8-cell meta grid (CPU / Mem / GPU / Pods …) reflows from
      `auto-fit` (which produced a single overflowing row) to a fixed
      2-column layout so every value stays on-screen.
    - The Jobs roster table header is hidden and each job row reflows
      from a 4-column grid to a stacked flex layout (identity on line 1,
      status + relative time on line 2). The "User" column is hidden
      because it was almost always empty for personal demos and was the
      first thing to overflow.
  - **Resource plane** (ACR · Storage) tightens for narrow viewports:
    - The Resource plane grid forces a single column so the two cards
      stack instead of squeezing into ~160 px each at the 760 px cutoff.
    - The dashboard section labels (`Cluster plane` / `Resource plane`
      / `Sidecar runtime`) shrink to 11 px with tighter margins.
    - Storage card subtitle drops the ` · resourceGroup` suffix so the
      account name doesn't get truncated by the right-hand controls.
    - The 4-cell Storage meta grid (Region / SKU / HNS / Network) and
      the 3-cell ACR summary (Login Server / SKU / Images built) both
      reflow to two columns. The ACR Login Server cell spans the full
      width on its own row so the FQDN isn't crushed.
    - Storage container rows wrap: the "updated …" relative-time line
      moves to its own row, leaving the access pill clear of overlap.
    - The BLAST Databases sub-section header wraps and hides the
      redundant "N/M catalog" counter (the "N downloaded" pill already
      conveys the count).
    - Storage warning banners (public endpoint, HNS disabled) use
      smaller padding and font so they stop dominating the card.
    - The "(optional)" suffix on optional ACR image rows is hidden;
      the existing `opacity: 0.65` already differentiates those rows.
    - The ACR header **Build** button and each row's per-image
      **Build** button are hidden on mobile. Mobile is read-only;
      kicking off image builds stays on desktop. In-flight banners
      (`BuildingBanner` / `ServerBuildingBanner` / `BuildDoneBanner`)
      remain visible because they're status info, not actions.
- Desktop / tablet layout is unchanged.

## API / IaC diff
None — pure frontend layout change.

## Files
- [web/src/pages/Dashboard/DashboardGrid.tsx](../../../web/src/pages/Dashboard/DashboardGrid.tsx):
  wrap `TerminalCard` in a `dashboard-hide-mobile` div; tag the Sidecar
  runtime section with the same class.
- [web/src/pages/Dashboard/DashboardHeader.tsx](../../../web/src/pages/Dashboard/DashboardHeader.tsx):
  add `dashboard-hide-mobile` to the workspace settings gear button and
  to the Help / Getting Started "?" re-open button.
- [web/src/pages/Dashboard/useGettingStartedReadiness.ts](../../../web/src/pages/Dashboard/useGettingStartedReadiness.ts):
  introduce `useIsMobileViewport()` (matchMedia `(max-width: 760px)`),
  short-circuit the auto-open effect on mobile, force-close the modal if
  the viewport shrinks while it is open, and make `reopenGettingStarted`
  a no-op on mobile.
- [web/src/components/cards/ClusterCard/AddClusterButton.tsx](../../../web/src/components/cards/ClusterCard/AddClusterButton.tsx):
  tag both pill and dashed variants with `dashboard-hide-mobile`.
- [web/src/components/cards/ClusterPulse/PulseActions.tsx](../../../web/src/components/cards/ClusterPulse/PulseActions.tsx):
  tag the right-side Stop/Delete action group with `dashboard-hide-mobile`.
- [web/src/components/cards/ClusterPulse/atoms.tsx](../../../web/src/components/cards/ClusterPulse/atoms.tsx):
  split the `PulseStat` label into its own `<span class="pulse-stat-label">`
  so CSS can hide just the label text while keeping the icon and value.
- [web/src/components/cards/ClusterPulse/PulseMetaGrid.tsx](../../../web/src/components/cards/ClusterPulse/PulseMetaGrid.tsx):
  tag the grid with `pulse-meta-grid` so mobile CSS can override
  `grid-template-columns` to a 2-column layout.
- [web/src/components/cards/ClusterPulse/JobsSection.tsx](../../../web/src/components/cards/ClusterPulse/JobsSection.tsx):
  tag `JobsTableHeader` with `pulse-jobs-header` and the user header span
  with `pulse-job-user`; tag the skeleton job rows with
  `pulse-job-row pulse-job-skeleton`.
- [web/src/components/cards/ClusterPulse/JobLine.tsx](../../../web/src/components/cards/ClusterPulse/JobLine.tsx):
  tag each rendered job row with `pulse-job-row` and the user cell with
  `pulse-job-user`.
- [web/src/theme/dashboard-layout.css](../../../web/src/theme/dashboard-layout.css):
  add `.dashboard-hide-mobile` and a cluster-pulse mobile block inside the
  existing `@media (max-width: 760px)` block (label hide, meta grid 2-col,
  jobs header hide, job row reflow + user-cell hide). Append a Resource
  plane block that forces 1-col, compacts section labels, reflows
  `dv3-cell-grid--4` / `--3` to 2 columns with a full-width first cell
  for ACR Login Server, wraps container rows, wraps the BLAST DB head
  while hiding the catalog counter, compacts `.storage-warning`
  banners, and hides `.acr-optional-tag`.
- [web/src/components/cards/StorageCard.tsx](../../../web/src/components/cards/StorageCard.tsx):
  wrap the subtitle's ` · resourceGroup` suffix in `<span
  class="storage-subtitle-rg">` so it can be hidden on mobile while
  desktop keeps the full identity.
- [web/src/components/cards/storage/StorageWarnings.tsx](../../../web/src/components/cards/storage/StorageWarnings.tsx):
  tag both banner divs with `storage-warning`.
- [web/src/components/cards/AcrCard/AcrImageRow.tsx](../../../web/src/components/cards/AcrCard/AcrImageRow.tsx):
  tag the optional-suffix span with `acr-optional-tag`.
- [web/src/components/Layout.css](../../../web/src/components/Layout.css):
  reset `.layout__nav { margin-left: 0 }` inside the `@media (max-width:
  1320px)` block. Without this, the desktop rule's `margin-left: 16px`
  leaked into the mobile drawer, so `transform: translateX(-100%)` only
  shifted the drawer by its own width but the 16 px margin pushed its
  right edge back into the viewport — a visible vertical strip on the
  left side that covered content while the menu was "closed".
  Also switch `.layout` from `min-height: 100vh` to `100dvh` (with a
  `100vh` fallback) so the bottom of the page is reachable on mobile
  browsers with retractable address bars. The large-viewport `100vh`
  value caused the last card to sit behind the iOS Safari / Chrome
  Android chrome.
- [web/src/theme/dashboard-layout.css](../../../web/src/theme/dashboard-layout.css):
  inside the `@media (max-width: 760px)` block, add `padding-bottom:
  calc(72px + env(safe-area-inset-bottom, 0px))` on
  `.dashboard-workspace`. The last visible card on mobile is the
  Storage card, whose `BlastDbSection` fetches its chip list async — if
  the user scrolls to the bottom before that fetch resolves, the page
  height jumps and the new chip rows render right at the visible edge
  (and in narrow desktop windows, behind the OS taskbar). The extra
  gutter keeps the last row clear of the browser/OS chrome.

- [web/src/components/cards/ClusterPulse/PulseRowSummary.tsx](../../../web/src/components/cards/ClusterPulse/PulseRowSummary.tsx):
  wrap the three `<PulseStat>` (Submits 15m / Active / Pressure) in a
  `<div class="pulse-row-stats">` and adjust `gridTemplateColumns`
  accordingly; tag the API-p95 sub-line `<span>` with
  `pulse-row-subline`. Both are hidden on mobile because the values are
  duplicated in the `PulseMetaGrid` immediately below, so the header
  row was confusing on a phone (three nearly-unlabelled numbers).
- [web/src/components/ClusterItem/DatabaseChipStrip.tsx](../../../web/src/components/ClusterItem/DatabaseChipStrip.tsx):
  tag the `warming · ready · failed` legend span with
  `cluster-db-legend` so mobile CSS can hide it. Each chip already
  shows the same colour next to the db name, so the legend is pure
  noise on a narrow screen.
- [web/src/theme/dashboard-layout.css](../../../web/src/theme/dashboard-layout.css):
  inside the `@media (max-width: 760px)` block, add hide rules for
  `.pulse-row-stats`, `.pulse-row-subline`, and `.cluster-db-legend`.

- [web/src/components/cards/ClusterPulse/JobLine.tsx](../../../web/src/components/cards/ClusterPulse/JobLine.tsx):
  add a 3-px phase-coloured left accent border on every job row so the
  pass/fail state scans vertically without reading the pill; replace the
  static 7-px phase dot with a `Loader2 spin` icon when the job is
  active so RUNNING/PENDING jobs are visibly live. Tag the status pill
  (`pulse-job-status-pill`), the time block (`pulse-job-timeblock` +
  `pulse-job-timeago` / `pulse-job-duration` children) so mobile CSS
  can bump their fonts.
- [web/src/components/cards/ClusterPulse/JobsSection.tsx](../../../web/src/components/cards/ClusterPulse/JobsSection.tsx):
  tag the "Jobs" eyebrow with `pulse-jobs-label` and append a
  `· N` count badge when the roster is non-empty; tag the inline
  caption with `pulse-jobs-caption`; convert the `N unknown` and
  `N failed / 15m` inline spans into `pulse-jobs-chip` chips so the
  most actionable signals look like chips on mobile; tag the "+N more"
  pill with `pulse-jobs-more-btn` so it can stretch full-width on
  mobile. The header row now allows `flex-wrap: wrap` so the chips
  drop to their own line on narrow screens.
- [web/src/theme/dashboard-layout.css](../../../web/src/theme/dashboard-layout.css):
  inside the `@media (max-width: 760px)` block, add Jobs-section font
  bumps and signal promotion: section label 12 px (was 10), caption
  11.5 px (was 10), chip background fill for unknown/failed, status
  pill 11 px + 3×8 padding + 700 weight (was 9 px / 1×5 padding /
  600), identity title 13 px (was 11.5), identity meta 11 px (was 10),
  time-ago 11.5 px bold (was 10), duration 10.5 px (was 9), "More
  jobs" pill stretches full-width with 12.5 px text and 8 px padding.

- [web/src/components/Layout.tsx](../../../web/src/components/Layout.tsx) +
  [web/src/components/Layout.css](../../../web/src/components/Layout.css):
  mobile nav drawer polish.
  - Add a `layout--mobile-nav-open` modifier on the layout root so CSS
    can drive sibling visibility while the drawer is open.
  - Inside the `@media (max-width: 1320px)` block, hide every topbar
    child other than the hamburger, logo, and nav while the drawer is
    open (`.layout--mobile-nav-open .layout__topbar > *:not(...)`).
    Previously the cluster-status pill, Live indicator, and the
    theme / help / account buttons all stayed visible behind the
    drawer header, which split the user's attention.
  - Hug-content the active nav item on mobile (`align-self:
    flex-start`) so the highlight pill matches the desktop look
    instead of stretching the entire drawer width — the active row no
    longer looks "wider" than the inactive ones.
  - Strengthen the drawer's top edge (`border-top` now uses
    `--border-medium`; add an inset shadow) so the drawer reads as a
    distinct surface rather than blending into the topbar.
- Cluster detail modal becomes a full-screen sheet on mobile and gets a
  larger close button:
  - `web/src/components/ClusterDetailModal/DetailsModal.tsx` —
    tag the backdrop with `cluster-detail-backdrop` and the modal
    root with `cluster-detail-dialog` (desktop sizing is unchanged;
    it stays inline-styled at `maxWidth: 1180,
    width: calc(100vw - 48px), maxHeight: 92vh`).
  - `web/src/components/ClusterDetailModal/ModalHeader.tsx` — tag the
    X button with `cluster-detail-close` and add `aria-label="Close
    cluster detail"` so the touch-target promotion below has an
    accessible name.
  - `web/src/theme/dashboard-layout.css` (`@media (max-width: 760px)`):
    `.cluster-detail-backdrop { padding: 0 !important; align-items /
    justify-items: stretch !important; }` (the explicit class avoids a
    `:has()` dependency and the stretch overrides the parent's
    `place-items: center` which otherwise lets the grid item shrink
    back to its natural width), `.cluster-detail-dialog { width: 100vw
    !important; height: 100dvh !important; max-width / max-height:
    100vw / 100dvh !important; min-width: 0 !important; margin: 0
    !important; border-radius: 0 !important; }`, and
    `.cluster-detail-close { min-width: 44px; min-height: 44px;
    padding: 10px 12px !important; }` with a 22px icon override. The
    `!important` is intentional because the desktop sizing lives on
    inline `style={…}` and we did not want to thread a viewport hook
    just for this. The selectors are scoped to this modal only.

## Validation
- `cd web && npm run build` (Vite type-check + bundle).
- Visual: open `http://127.0.0.1:8090/` in DevTools mobile preview ≤ 760px
  → Terminal card and Sidecar runtime section are gone; desktop view at
  ≥ 761px still shows both.
