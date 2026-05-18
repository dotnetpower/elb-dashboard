# 2026-05-18 — UI/UX Phase C (160 round-two derivations)

> Pure text deliverable: no code change.
> Round-one covered URL sync, indicators, banners, gates, deep links, sidebars
> and other "basics" (see `2026-05-18-ui-ux-phase-a.md`). Round two
> deliberately moves into the second-order surfaces — performance, motion,
> error recovery, observability, mobile, theming, print/export, keyboard,
> undo/redo, batch, a11y depth, security UX, privacy/telemetry, onboarding,
> contextual help, advanced search, personalization, collaboration.
>
> Each item is sized so a single change note can take it from idea to
> implementation. Numbering restarts per menu and uses the menu prefix used
> in phase A (D=Dashboard, N=New Search, J=Jobs, R=Results, C=Custom DB,
> L=Lab Tools, T=Terminal, A=API).

---

## 1. Dashboard (D21–D40)

- **D21 — `prefers-reduced-motion` audit.** Disable the radial pulse on
  RefreshRing and the skeleton shimmer when the OS opts out; keep colour
  cues so meaning is preserved.
- **D22 — Card pin & reorder.** Let the user pin frequently-used cards to
  the top and drag-reorder the remaining grid; persist to localStorage
  under `dashboard.layout.v2`.
- **D23 — Per-card refresh interval override.** Each MonitorCard already
  has a refresh ring; allow long-press / right-click to pick
  15s/30s/1m/5m for that card only, persisted per user.
- **D24 — "Why is this card empty?" contextual help.** Hover on an empty
  card surfaces a popover with the exact RBAC role / configuration needed,
  cross-linked to docs/auth.md and the SetupWizard step.
- **D25 — Pause polling when tab hidden.** Listen to
  `document.visibilitychange` and suspend `useQuery` refetch loops; resume
  with an immediate fetch on return.
- **D26 — Compact / expanded density toggle.** Switch between three
  densities (compact / regular / roomy) for users on small laptops vs.
  4K monitors.
- **D27 — Card-level error budget.** When a single card fails 3 times in
  10 minutes, automatically demote its polling to 5-minute interval and
  show a recovery affordance instead of hammering ARM.
- **D28 — Last-error timeline.** A small clock-icon menu per card listing
  the last 5 errors with timestamps, so an intermittent 502 isn't lost.
- **D29 — Cross-card correlated alerts.** If AKS is "stopped" and Jobs
  card is empty, render one combined banner ("Cluster off — jobs will
  resume after start") instead of two independent degraded states.
- **D30 — Export card payload.** Per-card "Copy as JSON" / "Download .json"
  button for sharing screenshots in incident threads.
- **D31 — Print-friendly stylesheet.** `@media print` styles that strip
  glass blur, expand all collapsibles, and add a generated cover page.
- **D32 — High-contrast theme.** Provide an alternate token set with
  WCAG AAA contrast for users with low vision; toggle from settings.
- **D33 — Light-mode polish.** Audit glass tokens for a sun-mode variant
  (off by default) for shared meeting-room displays.
- **D34 — Mobile / tablet breakpoint.** Stack monitor cards single-column
  below 720 px; convert the action chips to a bottom sheet.
- **D35 — Keyboard shortcut palette.** `?` opens a command palette listing
  all keyboard shortcuts (G then J = Jobs, G then T = Terminal, R = refresh).
- **D36 — Onboarding tour.** First-visit guided overlay (1-shot) that
  walks through cards 1→6 and ends on the Setup wizard.
- **D37 — Setup status checklist.** Sticky right-rail panel that lists
  unfinished setup steps ("RBAC missing on ACR", "OpenAPI image not built")
  with one-click jumps.
- **D38 — Telemetry consent banner.** Explicit opt-in for client-side
  performance telemetry (Vitals) sent to App Insights, with revoke flow
  and a "view what we collect" link.
- **D39 — Per-card observability metrics.** Tooltip on RefreshRing shows
  median / p95 latency over the last hour computed from
  `useQuery.dataUpdatedAt` deltas.
- **D40 — Collaboration share link.** Generate a deep link
  `?sub=…&rg=…` (subscription scope only, no secrets) so a teammate
  opening it lands on the same configuration.

---

## 2. New Search (N21–N40)

- **N21 — Resumable upload for query FASTA.** Block uploads of large
  query files into chunks; resume on socket drop without losing the
  staged blob.
- **N22 — Local cost estimator.** Compute estimated compute hours +
  storage IO + ACR pull traffic before submit, broken down per node SKU.
- **N23 — Side-by-side draft comparison.** When a draft exists, show a
  diff with the last successful submission's parameters so the user sees
  what's different.
- **N24 — Pre-flight cache.** Cache pre-flight results for 60 s so
  flipping back and forth between summary and editing doesn't re-run the
  expensive AKS/storage probes.
- **N25 — Saved presets.** Named parameter presets (DB, e-value, max
  targets, output format) that can be applied with one click.
- **N26 — Template gallery.** Curated query templates per use case
  (16S rRNA, COVID variant, metagenomics).
- **N27 — Validation linter for FASTA.** Inline warnings for ambiguous
  bases, too-short sequences, or duplicate IDs before submit.
- **N28 — Cost / time guardrail dialog.** If estimated cost exceeds a
  user-configurable threshold (e.g. $5), require an explicit "I'm sure"
  confirmation.
- **N29 — Run-as scheduled.** Pick "run now" vs. "run at <time>" vs.
  "run when cluster is idle"; queue via Celery beat.
- **N30 — Resource lock indicator.** If another submission is running and
  the AKS node pool is at capacity, show queued position + ETA.
- **N31 — Sandbox dry-run.** "Dry run" mode that splits + plans but
  doesn't submit any K8s jobs; reports the planned shard count and the
  estimated cost only.
- **N32 — Sticky parameter explanations.** A right-side help drawer that
  always shows the docs for the field currently focused.
- **N33 — Recent values dropdown.** For e-value / max-targets fields,
  show the last 5 distinct values the user actually used.
- **N34 — Search-time DB freshness check.** Warn if the selected BLAST
  DB has not been refreshed in N days, with a one-click "rebuild DB"
  shortcut to the Custom DB page.
- **N35 — Submit history button.** Inline "view my last 5 submits" link
  in the footer, opens a popover not a new page.
- **N36 — Keyboard submit.** ⌘/Ctrl-Enter to submit when canSubmit is
  true; the existing tooltip already names the requirement.
- **N37 — Mobile-friendly textarea.** Auto-grow + safe-area padding for
  iPad/Android tablet use in the lab.
- **N38 — Draft scope picker.** Save the draft to per-tab session
  storage (current) vs. localStorage (cross-tab) vs. server-side per-user
  draft (cross-device). Pick at footer.
- **N39 — File picker improvements.** Drag-drop overlay highlights the
  entire form, not just the file input; reject non-FASTA MIME types early.
- **N40 — Onboarding sample submission.** First-time users see a "Try
  with example query" CTA that pre-fills a tiny FASTA against a sample
  DB so they can validate the pipeline without leaving the page.

---

## 3. BLAST Jobs (J21–J40)

- **J21 — Bulk delete.** Select multiple jobs, confirm once, queue a
  Celery task per row; respect concurrency limits.
- **J22 — Bulk export.** Selected jobs → ZIP of results manifests +
  parameter sheets.
- **J23 — Saved views.** Pick filter + search + grouping + columns; save
  as named "view" (e.g. "Failed yesterday", "My nightly runs").
- **J24 — Column chooser.** Toggle elapsed-time / cost / shard count /
  DB version columns on/off; persisted per user.
- **J25 — Status-change toast log.** Optional toast when a job a user
  owns transitions to Completed or Failed, even when the user is on a
  different page.
- **J26 — Status-change browser notification.** Same as above using the
  Web Notifications API, opt-in.
- **J27 — Quick-filter from row.** Click on a row's DB pill / region /
  cluster chip to filter to "same as this row".
- **J28 — Inline retry / re-submit.** "Re-run this job" copies the
  parameters into a new draft in New Search.
- **J29 — Group-by toggle.** Switch between "by date" (current),
  "by status", "by DB", "by submitter".
- **J30 — Timeline scrubber.** Scroll horizontally through last 30 days,
  click a day to filter; visualises burst patterns.
- **J31 — Cost roll-up bar.** Show summed estimated cost for the
  currently-filtered rows at the top of the table.
- **J32 — Pagination + virtualization.** For users with > 500 jobs,
  switch to virtualized rendering instead of paging the API.
- **J33 — Server-side filter & search.** Push filter/search down to the
  backend so the SPA doesn't pull the full job list.
- **J34 — Optimistic delete with undo.** Local row strikethrough →
  delete after 5-second undo window; cancels the queued Celery task.
- **J35 — Bulk re-tag / annotate.** Add a free-text note column persisted
  in Table Storage for end-of-experiment write-ups.
- **J36 — Export to spreadsheet.** "Download CSV" with the visible
  columns and current filter; nothing the user can't already screenshot.
- **J37 — Owner-only filter.** "Show my jobs only" persistent toggle for
  shared-tenant deployments; respects `caller.object_id`.
- **J38 — Job age warning.** Row chip when a job has been "Running" for
  > 2× median; clickable to open the AKS pod logs deep-link.
- **J39 — Mobile row layout.** Below 720 px collapse non-essential
  columns into a "Details" expander.
- **J40 — Audit drawer.** Right-side drawer that shows the row's full
  Table Storage history events without leaving the page.

---

## 4. Job Results (R21–R40)

- **R21 — Stream large hit tables.** Render hits incrementally so a
  100-row result doesn't block the main thread.
- **R22 — Virtualised hit table.** Re-introduce `react-virtual` for
  > 1 000 hits so scrolling stays smooth.
- **R23 — Filter hits by e-value / identity.** Two-thumb sliders that
  filter without re-fetch.
- **R24 — Per-hit "view alignment".** Inline expandable alignment block
  using the existing pairwise renderer.
- **R25 — Highlight matching DB version.** Visual chip when the result
  used a DB version that is no longer the latest, with a re-run shortcut.
- **R26 — Copy-as-table.** Copy the visible hit table as TSV or
  Markdown for pasting into a lab notebook.
- **R27 — Export ASN.1 / pairwise / XML.** Existing outfmt selector +
  download path; new buttons in the toolbar instead of the menu.
- **R28 — Print one-pager.** `@media print` layout that drops chrome
  and prints just the parameter sheet + hit table + alignments.
- **R29 — Permalink to a hit.** `#hit-${accession}` deep-link expands
  the row + scrolls into view (mirrors API A2).
- **R30 — Annotation overlay.** Toggle GeneBank annotations on top of
  alignments when available (via the OpenAPI proxy).
- **R31 — Save annotated session.** Persist user annotations / highlights
  back to results blob (append-only) for resume.
- **R32 — "Open in IGV" launcher.** Generate an IGV session URL for
  hits whose accession resolves to a UCSC track.
- **R33 — Compare two jobs.** Side-by-side view of two submissions'
  hit tables, with a Venn-style overlap header.
- **R34 — Pipeline timing breakdown.** Stacked bar of provisioning /
  DB download / split / search / merge so the user sees where time went.
- **R35 — Cost actuals.** Replace the estimate with the actuals once the
  job is Completed; expose the delta.
- **R36 — Reproducibility manifest.** "Download manifest" that bundles
  parameters + DB version + image digests + caller identity for
  archival.
- **R37 — Share-this-result link.** Read-only deep link a teammate can
  open (no PII; respects RBAC server-side).
- **R38 — Storage-locked recovery polish.** Existing
  `StorageLockedPanel` should additionally surface the IP-allowlist
  helper script command line and a one-click "Copy command".
- **R39 — Streaming stderr tail.** Live tail of any `merge` errors via
  the terminal_exec server while results are being assembled.
- **R40 — Empty-result hint.** When 0 hits, distinguish "actually 0
  hits" from "filter excluded all hits" — show both states clearly.

---

## 5. Custom DB Builder (C21–C40)

- **C21 — Resumable FASTA upload.** Same chunking story as N21; large
  custom DBs are the biggest upload pain point.
- **C22 — Background build with notify.** Detach the build step from
  the page so the user can navigate away and get a notification on
  completion.
- **C23 — Validation report.** After upload, show count of sequences,
  total length, ambiguous-base ratio, duplicate IDs — before the user
  pays the makeblastdb cost.
- **C24 — Cost estimate before build.** Bytes → blob storage cost +
  estimated build CPU minutes.
- **C25 — Versioned DB ID.** Auto-append `vN` suffix when re-publishing
  the same name so previous submissions can still reference the old
  version.
- **C26 — Side-by-side DB diff.** When re-building, diff the new
  sequence set against the previously published version.
- **C27 — Build logs viewer.** Stream `makeblastdb` stderr/stdout via
  the terminal_exec server with collapsible sections per phase.
- **C28 — Retry from any step.** If "Build & publish" fails, allow
  retry from that step alone without re-uploading the FASTA.
- **C29 — Saved configurations.** Persist (db type, masking options,
  description) per user so a recurring weekly build is one click.
- **C30 — Database catalogue page.** Link out to a Lab Tools subpage
  that lists all custom DBs with metadata, build date, size, and an
  "use in new search" CTA.
- **C31 — Soft-delete with restore window.** 7-day restore window for
  deleted DBs; permanent only after explicit purge.
- **C32 — Multi-FASTA merge wizard.** Upload N files, dedupe IDs,
  build as one DB.
- **C33 — Schedule weekly rebuild.** Celery beat schedule for the
  saved config; toggle on/off in the page.
- **C34 — Quota awareness.** Warn when the Storage account is
  approaching its quota or maxIngressMbps before allowing the upload.
- **C35 — Mobile read-only view.** Show recent builds and statuses on
  mobile (no upload), so a lab manager can check without a laptop.
- **C36 — Keyboard "Next" navigation.** ⌘/Ctrl-→ moves to the next
  wizard step when its `done` predicate is true.
- **C37 — Onboarding tour.** First-visit tour that explains the 3
  steps and links to the elastic-blast docs for FASTA format.
- **C38 — In-page docs sidebar.** Sticky right-rail that pulls the
  matching `azure-prereq.md` section based on the current step.
- **C39 — Privacy classification.** Pick "internal / collaborator /
  public" tag; warn if user picks "public" with a non-empty IP allowlist.
- **C40 — Audit log link.** Surface the DB's append-blob audit history
  with caller identity hashes for compliance evidence.

---

## 6. Lab Tools (L21–L40)

- **L21 — Tool catalogue search.** Top-of-page input that filters the
  Cost / Preprocess / Primer / Taxonomy / Schedules / DbVersions / Audit
  tabs by tool name.
- **L22 — Keyboard tab navigation.** Left/Right arrow keys move between
  tabs when focus is in the tab strip.
- **L23 — Recently used tools.** A small "Recent" tab that lists the
  last 5 tabs the user actually used.
- **L24 — Per-tool last-error indicator.** Red dot on a tab when its
  underlying endpoint last returned a degraded payload.
- **L25 — Tools status board.** A "tool health" header strip listing
  which tools are backed by real Celery tasks vs. still 503-stubs.
- **L26 — Pin a tool as default.** Set any tab as the landing tab,
  overriding the global DEFAULT_TAB.
- **L27 — Tool-level dark/light variants.** Some tools render graphs;
  pick a matching theme per tool.
- **L28 — Cost tool — what-if simulator.** Slider for node count + SKU
  + hours, real-time recompute.
- **L29 — Cost tool — month-to-date roll-up.** Sum of estimated and
  actual costs across all completed jobs this month.
- **L30 — Preprocess tool — sequence stats panel.** Length distribution
  histogram + GC content for staged queries.
- **L31 — Primer tool — Tm calculator.** Quick melting-temperature
  calculator with salt + dNTP inputs.
- **L32 — Taxonomy tool — TaxID lookup.** Input a TaxID, surface
  lineage + child counts.
- **L33 — Schedules tool — calendar view.** Replace the table with a
  week/month calendar of beat schedules.
- **L34 — Schedules tool — disable all toggle.** "Pause all schedules"
  master switch (with confirmation) for incident response.
- **L35 — DbVersions tool — diff.** Compare two DB versions' size,
  sequence count, build date.
- **L36 — Audit tool — full-text search.** FTS-style search over the
  append-blob audit stream with date / actor filters.
- **L37 — Audit tool — export.** Download the filtered audit as CSV.
- **L38 — Audit tool — anomaly highlights.** Visually flag rows where
  the caller's IP / role differs from their usual pattern.
- **L39 — Onboarding tour per tool.** First-visit tooltip per tab when
  the user opens it for the first time.
- **L40 — Cross-tool deep links.** Each tool's row in another tool can
  link back (e.g. Cost row → Schedules' matching beat entry).

---

## 7. Browser Terminal (T21–T40)

- **T21 — Session list & multiplex.** Multiple labelled tmux windows
  side by side with a tab strip.
- **T22 — Reconnect-with-state.** On disconnect, replay the last N
  lines of scrollback before resuming live (uses tmux's `capture-pane`).
- **T23 — Search in scrollback.** ⌘/Ctrl-F search across the
  scrollback buffer.
- **T24 — Copy / paste polish.** "Copy on select" toggle and an
  explicit paste-from-clipboard menu (some lab browsers block automatic
  paste).
- **T25 — Slash-command suggestions.** Type "/" to get a popover of
  common `elastic-blast` and `kubectl` recipes.
- **T26 — Pre-baked recipe palette.** "Run `elastic-blast status`
  every 10 s" as a one-click action.
- **T27 — Output capture as artefact.** "Save last command output to
  Files share" button so terminal logs don't have to live in scrollback.
- **T28 — Font / contrast settings.** xterm.js theme + font-size picker
  with live preview.
- **T29 — Reduced-motion cursor.** Disable cursor blink when
  `prefers-reduced-motion`.
- **T30 — Mobile-friendly soft keyboard row.** Esc / Tab / arrows /
  pipe / Ctrl row above the keyboard for iPad use.
- **T31 — Session timeout policy.** Surface the remaining session TTL
  + warn 60 s before disconnect.
- **T32 — Per-keystroke audit toggle.** Off by default; when enabled
  every keystroke is timestamped to the audit blob (incident-response
  use only).
- **T33 — Onboarding banner.** First-time hint to run
  `az login --use-device-code` (complements T2 indicator).
- **T34 — In-terminal help.** `?` keystroke opens a side drawer
  listing all `elastic-blast` / `elb` commands.
- **T35 — Active-job ribbon.** Sticky strip showing the currently
  active `elastic-blast` submission with progress.
- **T36 — Restart-terminal confirmation.** Confirm dialog with explicit
  "This drops your tmux session" warning before sidecar restart.
- **T37 — Reset-home guard.** Multi-step confirmation + typed-name
  guard for resetting `/home/azureuser` (drops cached az login).
- **T38 — Shareable session id.** "Share session id" copy button so
  collaborators can join with `tmux attach -t <id>` (RBAC enforced).
- **T39 — Telemetry: command exit codes.** Aggregate exit-code chart
  over last 24h for the cockpit.
- **T40 — Privacy redaction.** Auto-redact tokens / SAS strings
  rendered in the terminal output (mirrors the api-side sanitiser).

---

## 8. API Reference (A21–A40)

- **A21 — Operation history.** Per-endpoint "last 5 calls I made" log
  with status + duration; replays one-click.
- **A22 — Save request as preset.** Name + persist a fully-populated
  body so the user can re-run with one click.
- **A23 — Compare two responses.** Side-by-side JSON diff between two
  recent executions of the same endpoint.
- **A24 — Query playground panel.** A `/api/blast/search` style
  playground (akin to GraphiQL) for ad-hoc exploration.
- **A25 — Per-endpoint cURL generator.** Auto-generate a copy-pasteable
  cURL with the live token and params filled in.
- **A26 — Per-endpoint code samples.** Toggle: cURL / TypeScript fetch /
  Python httpx.
- **A27 — OpenAPI version banner.** Show spec version + last refresh
  time at the top of the sidebar, with a manual refresh button.
- **A28 — Mark deprecated endpoints.** Cross-reference `deprecated:
  true` from the spec and render a strikethrough in the sidebar list.
- **A29 — Tag descriptions inline.** Surface the OpenAPI tag
  `description` in the sidebar as a hover popover.
- **A30 — Try-it sandbox / production toggle.** Run against the live
  cluster or against a recorded fixture (no side effects).
- **A31 — Saved environments.** Cluster A / Cluster B / local —
  switchable from the sidebar.
- **A32 — Request schema linter.** Inline warnings when the user's
  JSON body doesn't match the OpenAPI schema before they hit Try-It.
- **A33 — Response schema validator.** Highlight fields in the response
  that don't match the spec — surfaces drift between spec and runtime.
- **A34 — Error catalogue.** A dedicated tab listing all error
  responses' `detail.code` values + remediation steps.
- **A35 — RBAC matrix overlay.** Tag each endpoint with the minimum
  RBAC role; filter "show me endpoints I can call".
- **A36 — Rate-limit indicator.** Show the per-endpoint quota (when
  the backend publishes one) with a current-usage bar.
- **A37 — Print-friendly spec dump.** Single-page printable spec
  view with `@media print` polish.
- **A38 — Onboarding for new endpoints.** "What's new since you last
  visited" diff: endpoints added / changed / removed.
- **A39 — Mobile read-only view.** Below 720 px hide the Try-It UI and
  render endpoints as a long-scroll spec viewer.
- **A40 — Collaboration / link-to-step.** Deep link a particular
  request (method + path + body preset id) so a teammate opening it
  lands in the playground pre-filled.

---

## Cross-cutting concerns (not yet split per menu)

For the next phase to schedule, the following are worth promoting from
the per-menu lists into shared infrastructure:

1. **Reduced-motion + high-contrast theme** (D21, D32, T29). One token
   set serves everyone.
2. **Mobile / tablet breakpoint pass** (D34, J39, T30, A39, R/N for
   read-only). Same media-query rules.
3. **Print stylesheet** (D31, R28, A37). Single `@media print` file
   in `web/src/theme/`.
4. **Resumable upload** (N21, C21). Shared chunked-upload hook.
5. **Onboarding tour engine** (D36, N40, C37, T33, L39, A38). Single
   stepper component driven by per-page configuration.
6. **Saved presets / personalisation** (J23, L26, N25, R31, A22, A31).
   Backed by user-scoped settings in Table Storage.
7. **Streaming / virtualisation** (J32, R21, R22). TanStack Virtual
   in one place.

Phase B (when scheduled) should pick the top 5 of these to land the
infrastructure once, then iterate the per-menu hookups.
