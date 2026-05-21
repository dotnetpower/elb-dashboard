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
    tag the backdrop with `cluster-detail-backdrop`, the modal
    root with `cluster-detail-dialog`, and the body scroll wrapper
    with `cluster-detail-modal-body` (desktop sizing is unchanged;
    the modal stays inline-styled at `maxWidth: 1180,
    width: calc(100vw - 48px), maxHeight: 92vh` with an inner
    `overflow-y: auto` body).
  - `web/src/components/ClusterDetailModal/ModalHeader.tsx` — tag
    the outer header with `cluster-detail-modal-header`, the stat
    cards grid (NODES / K8S / POOLS / OS) with
    `cluster-detail-modal-stats`, the X button with
    `cluster-detail-close`, and add `aria-label="Close cluster
    detail"`.
  - `web/src/components/ClusterDetailModal/NodePoolsTable.tsx` —
    tag the table wrapper with `cluster-detail-pools-wrap` so it
    can scroll horizontally on phones (7 columns don't fit).
  - `web/src/components/WarmupSection.tsx` — tag the DbRow grid
    with `warmup-db-card` and its action group with
    `warmup-db-card__actions`. The 3-column grid (name | status |
    actions) was overflowing on phones, leaving the Warm / Release
    buttons partially visible at the right edge. On mobile the grid
    collapses to a single column and the action group is hidden —
    those destructive operations stay on desktop where the user has
    a proper pointer.
  - `web/src/components/ClusterDiagnostics/K8sNodesSection.tsx` and
    `K8sPodsSection.tsx` — tag the scrollable table wrappers with
    `k8s-nodes-table-wrap` / `k8s-pods-table-wrap`. On mobile the
    horizontal scroll is dropped and non-essential columns are
    hidden so the rest fits without a scrollbar: Nodes keeps NAME +
    STATUS (drops VERSION / IP / OS / RUNTIME); Pods keeps NAME +
    READY + STATUS + Logs button (drops NS / RESTARTS / NODE).
    The hidden columns remain in the DOM for desktop and screen
    readers.
  - `K8sPodsSection.tsx` — Logs button now uses `inline-flex` and
    `white-space: nowrap` (plus a `k8s-pods-logs-button` class with
    slightly larger mobile padding / 28px min-height) so the label
    no longer wraps to "Lo / gs" inside the narrow action column.
  - `PodLogsDialog.tsx` + CSS — tag the backdrop / dialog with
    `pod-logs-backdrop` / `pod-logs-dialog` and apply the same
    mobile fullscreen treatment as the cluster detail modal
    (`position: fixed; inset: 0`, 100vw × 100dvh, no border-radius,
    `overscroll-behavior: contain`). Previously the Pod Logs modal
    opened as a narrow centered card pushed up against the cluster
    detail modal's scroll position, leaving header / close button
    clipped.
  - `dashboard-layout.css` — generalized the fullscreen-on-mobile
    rule to all glass dialogs via
    `.glass-dialog-backdrop > .glass-card` (covers BlastDbModal,
    ConfirmDialog, PodLogsDialog, ClusterDetail DetailsModal,
    TaxonomyModal, QuerySection example dialog, and any future
    modal that follows the same backdrop-then-card structure).
    Per-modal classes (`cluster-detail-dialog`, `pod-logs-dialog`,
    …) now only carry behavior extras (e.g. whole-modal scrolling
    for the cluster detail). Desktop styling is untouched — the
    rules live inside the existing `@media (max-width: 760px)`
    block.
  - The same generic rule now also adds
    `padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 16px)`
    to the card so the last row of any modal's scrollable body
    isn't hidden behind mobile browser chrome (URL bar) or OS
    bottom strip (iOS home indicator, Windows taskbar overlapping
    the DevTools mobile preview). Inline `padding: 0` on each card
    is overridden because `!important` in the stylesheet trumps
    non-important inline styles. For cluster-detail-dialog this
    composes with the existing 72px body padding so users can
    scroll well clear of any browser/OS chrome.
  - Promoted the page-level bottom safe-area gutter from
    `.dashboard-workspace` to the universal `.layout__main` wrapper
    (still inside `@media (max-width: 760px)`). This fixes "last
    element hidden behind chrome" on every page — including the Run
    BLAST button at the bottom of the New Search form which was
    being covered by the Windows taskbar in DevTools mobile preview.
    The dashboard-specific rule was removed because `.layout__main`
    already wraps `.dashboard-workspace` and applying the same value
    twice would double-pad.
  - `cluster-detail-modal-body` now carries a `padding-bottom:
    calc(72px + env(safe-area-inset-bottom, 0px))` so the last DB /
    diagnostic card can scroll fully into view above the mobile
    browser chrome and the iOS / Android home-bar safe area —
    previously the bottom card was cut off by the OS / browser
    bottom strip.
  - `web/src/theme/dashboard-layout.css` (`@media (max-width: 760px)`):
    `.cluster-detail-backdrop { padding: 0 !important; align-items /
    justify-items: stretch !important; }`, `.cluster-detail-dialog
    { position: fixed !important; inset: 0 !important; width: 100vw
    !important; height: 100dvh !important; max-width / max-height:
    100vw / 100dvh !important; min-width: 0 !important; margin: 0
    !important; border-radius: 0 !important; overflow-y: auto
    !important; overflow-x: hidden !important; overscroll-behavior:
    contain !important; }` — the `position: fixed; inset: 0` pins
    the dialog directly to the viewport so the underlying dashboard
    can never peek through even when the scroll content is shorter
    than the viewport, and `overscroll-behavior: contain` blocks the
    rubber-band that was exposing the dashboard at the bottom edge.
    Then `.cluster-detail-modal-body { overflow: visible !important;
    flex: initial !important; padding: 12px !important; }` so the
    body is no longer the inner scroll container,
    `.cluster-detail-modal-header { padding: 10px 12px 12px
    !important; }` to compress the header, and
    `.cluster-detail-modal-stats { display: none !important; }` to
    drop the redundant NODES / K8S / POOLS / OS cards (those values
    still live in the Node Pools table inside the body, and in the
    cluster card's PulseMetaGrid). Plus `.cluster-detail-pools-wrap
    { overflow-x: auto !important; }`, the touch-target promotion
    of `.cluster-detail-close` (44×44 with a 22px icon), and
    `.warmup-db-card { grid-template-columns: 1fr !important; }`
    with `.warmup-db-card__actions { display: none !important; }`
    so the Warm / Release buttons no longer leak past the right
    edge on phones. The `!important` is intentional because the
    desktop sizing lives on inline `style={…}` and we did not want
    to thread a viewport hook just for this. The selectors are
    scoped to this modal only.

## Validation
- `cd web && npm run build` (Vite type-check + bundle).
- Visual: open `http://127.0.0.1:8090/` in DevTools mobile preview ≤ 760px
  → Terminal card and Sidecar runtime section are gone; desktop view at
  ≥ 761px still shows both.

## Hardening pass (critical review)

After the universal `.layout__main` safe-area promotion landed, a follow-up
critical review surfaced three regressions / gaps that the generic modal
rule missed:

1. **Double bottom padding inside the cluster-detail dialog.** Once the
   dialog itself became the scroll container (via
   `.cluster-detail-dialog { overflow-y: auto !important }`) and the
   generic `.glass-dialog-backdrop > .glass-card` rule started reserving
   `env(safe-area-inset-bottom) + 16px`, the body's pre-existing
   `padding-bottom: calc(72px + env(safe-area-inset-bottom))` stacked
   on top of that, producing roughly 88px + 2× safe-area of dead space
   after the last card. Reduced the body to `padding-bottom: 24px
   !important` so the only safe-area calc lives on the dialog (the
   scroll container) and the inner gutter stays a sensible 24 px.
2. **`KeyboardShortcuts` (`?` overlay) overflowed phones horizontally.**
   The inner dialog had an inline `width: 480` (no `min()` wrapper),
   so on any viewport narrower than 480 CSS pixels the modal scrolled
   the whole page sideways. Tagged the inner div with
   `className="shortcut-dialog-card"` and added a mobile rule that
   forces 100vw / 100dvh with the same safe-area gutter as
   `.glass-dialog-backdrop > .glass-card`. The backdrop already has
   `.shortcut-overlay`, so the only source change is a single new
   className on the inner card.
3. **`ProvisionModal` ("Create AKS Cluster") was a pinched centered
   card on phones.** It's an inline-styled portal so it never
   participated in the `.glass-dialog-backdrop` cascade. Added
   `className="provision-modal-backdrop"` and `className="provision-modal-card"`
   to its two divs and gave them the same fullscreen + safe-area
   treatment under the 760 px media query. Desktop styling is
   untouched because the inline `width: "min(760px, calc(100vw - 32px))"`
   still wins outside the media query.

Modals deliberately left as-is:
- `SettingsPanel` is a side drawer (`top: 0; right: 0; bottom: 0;
  width: min(520px, calc(100vw - 24px))`) with its own visual language,
  not a centered modal. On phones the 24 px gutter is intentional
  affordance, not a bug.
- `SidecarsCard` HTTP inspector is a dev/inspect tool surfaced behind
  an internal toggle and not part of the daily user surface; deferred.

## Hardening validation
- `cd web && npm run build` → `✓ built in 7.34s`, no type errors.
- Targets exercised in DevTools mobile preview (375 × 812):
  - `?` keyboard shortcut overlay now spans 100vw / 100dvh; the
    bottom of the "Resources" tab clears the OS strip.
  - Create AKS Cluster modal spans full width; the SKU group lists
    no longer require horizontal scroll inside a 760 px-wide cell.
  - Cluster detail dialog: scrolling to the bottom of the warmup
    list lands ~24 px above the last DB card instead of leaving an
    88 px empty band.

## Recent searches detail header (`BlastJobHeader`)

User reported the Recent searches detail page (`/blast/jobs/{id}`) was
visibly broken on phones: the long search title overlapped the action
buttons and the metadata grid's right-hand column (Submitted / Molecule
type / Region) was clipped past the viewport edge.

Root causes:
1. The action row was `flexWrap: "wrap"` with an unconstrained
   `<h1>{jobTitle}</h1>`. Long titles like
   `20260521-1200 MPXV F3L - NC_003310.1` never wrapped inside the h1,
   so the h1 forced its parent wider than the viewport and the
   `flex: 1` made the buttons share whatever was left over — they ended
   up drawn on top of the title.
2. The metadata `<dl>` used a 4-column grid
   (`min-content max-content min-content 1fr`). On a 360 px-wide screen
   the four tracks summed to more than the viewport, so every
   right-column value was clipped (only the labels "SUBMITTED",
   "MOLECULE", "REGION", "DATE" rendered with no value behind them).

Hardening:
- Added `className="blast-job-header"` to the header, plus
  `blast-job-header__title-row` and `blast-job-header__meta-grid` to
  the two affected wrappers (source change is three className adds —
  no inline-style edits, desktop layout untouched).
- New mobile rules in `dashboard-layout.css` inside
  `@media (max-width: 760px)`:
  - h1 → 18 px font, `overflow-wrap: anywhere`, `word-break: break-word`
    so the search title wraps cleanly instead of overflowing.
  - title row → stacks `flex-direction: column`, action buttons go
    full-width and centered so Cancel / Edit search / Save settings
    don't fight for inline space.
  - meta grid collapses to two columns (`max-content 1fr`); the
    spanning rows (DB title / description / snapshot, query
    description) use an attribute selector
    `dd[style*="span 3"] { grid-column: 1 / -1 !important }` so the
    inline `span 3` doesn't blow the grid out to 3 implicit tracks.
  - code values get `word-break: break-all` so the Search ID and
    cluster name wrap rather than push the grid wider.

Validation:
- `cd web && npm run build` → `✓ built in 11.11s`.
- DevTools mobile preview at 375 × 812 on
  `/blast/jobs/{id}?tab=descriptions`: title now wraps to ≤ 2 lines
  inside the visible width, buttons stack below it full-width, and
  every metadata field (Search ID / Submitted / Program / Database /
  Molecule type / Cluster / Region) renders its value without
  clipping.

## API Reference page (`/docs`) mobile optimization

### Motivation
The API Reference page (`web/src/pages/ApiReference.tsx`) was built
desktop-first: a sticky 240-280 px sidebar plus a fluid endpoint
column, a hero row with title + 3-stat chips + baseUrl pill + Swagger
UI button + Refresh button, endpoint cards with a 6-item header (method
badge + path + summary + copy-link + Try + chevron) and an expanded
body split into a `1fr 1fr` grid (description/params | try-it). On a
phone the sidebar squeezed the content column to ~80 px, the hero row
overflowed horizontally, endpoint paths wrapped onto the chevron, and
the expanded body became unreadable. User explicitly granted
permission to hide non-essential controls on mobile
("불필요한거는 안보여도될것 같아").

### User-facing change
On viewports ≤ 760 px the `/docs` page now:

- hides the sticky `ApiReferenceSidebar` (users scroll the tag
  sections instead);
- stacks `ApiHero` vertically: title block on top, action buttons
  below, with the long baseUrl pill and Swagger-UI external link
  hidden (only Refresh remains useful on a phone);
- hides the 3-stat row (Endpoints / Groups / Methods) — redundant
  vertical noise on small screens;
- collapses each `EndpointCard` header so the path occupies its own
  full-width line under the method badge, and drops the truncated
  summary preview and the rarely-used copy-link button;
- collapses the expanded body grid from `1fr 1fr` to a single column
  (description + parameters first, try-it form below);
- collapses each parameter row from `120 px / 60 px / 1fr` to a
  single column (name + description stacked), and hides the dedicated
  "type" cell (the `req` badge in the name cell still marks required
  params).

Desktop layout is untouched — all rules sit inside
`@media (max-width: 760px)` in
`web/src/theme/dashboard-layout.css`, and the only `.tsx` changes are
new anchor `className`s on existing wrappers (no inline styles
removed).

### Files changed
- `web/src/pages/ApiReference.tsx` — no edits (already had
  `api-reference-page` + `api-reference-layout` classes).
- `web/src/pages/apiReference/ApiHero.tsx` — added
  `api-hero__row`, `api-hero__actions`, `api-hero__stats`,
  `api-hero__base-url`, `api-hero__swagger`.
- `web/src/pages/apiReference/EndpointCard.tsx` — added
  `endpoint-card`, `endpoint-card__header`, `endpoint-card__summary`,
  `endpoint-card__copylink`, `endpoint-card__body`,
  `endpoint-card__param-row`.
- `web/src/theme/dashboard-layout.css` — new mobile block under
  the `BlastJobHeader` rules (~80 lines, all inside the existing
  `@media (max-width: 760px)`).

### Validation
- `cd web && npm run build` → `✓ built in 7.23s` (clean).
- Manually inspected the new CSS: every selector is scoped under
  `@media (max-width: 760px)` and uses `!important` only to beat
  inline desktop `style={...}` props; no rule leaks to desktop.

## Recent searches list page — table no longer pushes page out of viewport

### Motivation
On mobile widths the `/blast/jobs` (Recent BLAST searches) page was
extending past the viewport: the desktop-sized 5-column table inside
each `DateGroupSection` (`Job / User / Status / Time / Delete`) used
`whiteSpace: nowrap` on the User / Status / Time / Delete cells, so
the table's intrinsic min-content exceeded the phone viewport and
pushed the whole page wider than 100 vw, producing horizontal page
scroll and clipping the right edge of the content.

The table already lives inside a `.table-scroll` wrapper with
`overflow-x: auto`, which *should* have contained the overflow as
internal horizontal scroll. It didn't, because `.page-stack.jobs-page`
is a flex column and flex children default to `min-width: auto`
(content-based). That let each `DateGroupSection`'s root `<div>`
inflate to its table's intrinsic width, dragging the page with it.

### User-facing change
On `/blast/jobs` at any width, the per-group table now scrolls
horizontally **inside** its `.table-scroll` container instead of
expanding the page. On phones the page no longer scrolls sideways;
swiping the table reveals the User / Status / Time / Delete columns.
Desktop layout is unchanged because at wider widths the table
naturally fits inside the row and never needs to scroll.

### Files changed
- `web/src/theme/glass.css` — added `min-width: 0` to `.jobs-page`
  and `.jobs-page > *` so flex children can shrink and let
  `.table-scroll`'s existing horizontal scroll contain the table.
  Two lines of CSS, no new selectors, no inline-style edits.

### Validation
- `cd web && npm run build` → `✓ built in 11.32s` (clean).
- Live check at `http://127.0.0.1:8090/blast/jobs` with
  `.layout__main { max-width: 390px }` injected: `documentElement.scrollWidth`
  no longer exceeds the viewport width; `.table-scroll` reports
  `clientWidth ≈ 364 px`, `scrollWidth ≈ 602 px` (correctly contained
  with internal horizontal scroll); table bounding rect (578 px)
  stays clipped inside `.table-scroll`.

### Follow-up: table content was still clipped behind the internal scroll

The first pass only stopped the *page* from extending — it left the
table itself ~578 px wide inside the now-contained `.table-scroll`,
so on a phone the Status / Time / Delete columns were hidden behind
the (mostly invisible) internal horizontal scroll. User screenshot
confirmed "COMP…" was clipped at the right edge of the card.

Tightened the table so all visible columns actually fit at 360 px
container width (in `dashboard-layout.css` `@media (max-width: 760px)`,
`.jobs-page` block):

- hide the `User` column entirely (header + cells via `nth-child(2)`)
  — single-user workspaces always show `—` here anyway;
- shrink TD padding to `4 px`, font-size to `11 px`, drop the
  first/last cell side padding;
- override the inline `white-space: nowrap` on Status (`nth-child(3)`)
  and Time (`nth-child(4)`) TDs, and on the Job-title anchor, so cells
  can shrink — the title now wraps into 2 short lines instead of
  forcing the column to its intrinsic width;
- hide the Time TD's second sub-line ("Duration N m N s"); the
  headline "X ago" is enough on a phone;
- change `.jobs-page .table-scroll` to `overflow-x: hidden` on mobile
  (no longer needed since the table now fits) and tighten its inner
  padding to `8 px`.

Verified via DOM: at injected 390 px viewport, table now 348 px wide
(< 364 px container), `scrollWidth === clientWidth` on
`.table-scroll`, every row shows Job + Status + Time + Delete in
view. Build clean (`✓ built in 8.04s`). Desktop layout unchanged
(rules scoped to `@media (max-width: 760px)`).

### Follow-up #2: Today fit but Yesterday/older sections still overflowed

User reported Today rendered correctly but Yesterday (and older
sections) still clipped on mobile. Reproduced by injecting
`.layout__main { max-width: 390px }`: Today + This Week tables fit
at 348 px (< 364 px container) but Yesterday's table was 502 px.

Root cause: every Yesterday row has a long unbreakable title token
(`20260520-2309 MPXV F3L - NC_003310.1`) plus the `worker_lost` /
`elb-cluster` inline-flex badges in the meta line. Under
`table-layout: auto` the column's `min-content` was the widest of
those badges (which themselves are inline-flex single-token boxes
that don't honor `word-break`), so the Job column refused to shrink
below ~340 px and the whole table inflated to 502 px regardless of
`white-space: normal`. Today's rows happened to have shorter
titles so they squeezed under the limit by luck.

Switched the table to `table-layout: fixed; width: 100%` on mobile
and pinned the right-side columns to explicit widths
(`Status 78 px`, `Time 72 px`, `Delete 32 px`). The Job column now
absorbs the remaining ~178 px and the inline-flex badges wrap or
clip inside that column instead of dictating table width.

Verified: all three sections (`Today`, `Yesterday`, `This Week`)
report `tableW = 348 px`, `scrollWidth === clientWidth = 364 px`,
no horizontal scroll. Build clean (`✓ built in 10.32s`). Desktop
layout untouched.

### Follow-up #3: BLAST Databases modal leaked the dashboard behind it on mobile

User reported that on mobile, opening the BLAST Databases modal from
the dashboard and scrolling exposed the dashboard's Storage card
(`results` container row, the inline BlastDbSection summary) **below**
the modal, as if the modal didn't fully cover the viewport.

Root cause: the mobile fullscreen rule used `height: 100dvh !important`
(plus matching `max-height`/`min-height`). On engines that don't
understand the dynamic-viewport unit (`dvh`), the entire declaration
becomes invalid and is dropped — leaving the card on its inline
`maxHeight: 86vh` with `height: auto`, so the card shrinks to its
content and the body behind the backdrop becomes visible underneath.

Fix: drop the explicit height entirely and rely on `position: fixed`
+ `inset: 0` to pin all four edges to the viewport (which gives a
fully-covering box on every engine, no `dvh` needed), and override
the inline `maxHeight` with `max-height: none !important`. The same
treatment is applied to the bespoke modal cards
(`.shortcut-dialog-card`, `.provision-modal-card`), where `inset: 0`
isn't available because they're inline-styled portals, so they get a
`100vh` fallback declared *before* `100dvh` (older engines apply
`100vh`, modern engines override with `100dvh`).

Verified by injecting the new rule outside `@media` on the desktop
viewport (1914 × 897): with `inset: 0` + `max-height: none`, the
BlastDb modal card resolves to `0,0 1914×897` — `coversFullViewport:
true`, dashboard cards no longer leak through. Build clean
(`✓ built in 10.85s`). Desktop layout unchanged (all rules scoped
to `@media (max-width: 760px)`).
