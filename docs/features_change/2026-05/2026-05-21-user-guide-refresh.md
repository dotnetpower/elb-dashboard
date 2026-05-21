# 2026-05-21 — User Guide refresh

## Motivation

The `User guide` section under `docs/user-guide/` rendered duplicated
content on five of seven pages. Two stub templates from earlier
iterations had been concatenated into `index.md`, `new-search.md`,
`jobs.md`, `results.md`, and `terminal.md` without ever being merged,
so the live site rendered two `# Heading` blocks back-to-back and
"What To Explain" TODO lists instead of real prose. The Dashboard
page was technically free of duplication but was thinner than the
rewritten siblings (no workspace-selector section, no card-state
table, no sidecar runtime explanation). API Reference was already
complete.

A quick check of the rendered HTML at
`http://127.0.0.1:8012/elb-dashboard/user-guide/` confirmed two
`User Guide` titles and two `Workflow` / `Planned Pages` blocks on
the index alone.

## User-facing change

- `docs/user-guide/index.md` — replaced with a single, clean
  introduction. Adds a "Pages at a Glance" table aligned with the
  actual screenshot filenames declared in
  `docs/screenshot-capture-manifest.json`, a "Try Without An Azure
  Subscription" hand-off to the UI Preview, and fixes the broken link
  to the Screenshot Workflow (was `../documentation/...`, now
  `../contributor-guide/...`).
- `docs/user-guide/new-search.md` — full rewrite to match the actual
  `/blast/submit` page: 7-step stepper, program / query / search-set /
  taxonomy / runtime / params sections, preflight + command preview +
  draft auto-save behaviour. Screenshot target wired to
  `new-search-desktop.png`.
- `docs/user-guide/jobs.md` — full rewrite to match the actual
  `/blast/jobs` page: header counts, status chip filters + search,
  date-grouped rows with the data each row shows, empty / loading /
  degraded states, and the link into Results. Screenshot target wired
  to `jobs-desktop.png`.
- `docs/user-guide/results.md` — full rewrite covering the NCBI-style
  job header, the six tabs (Descriptions / Graphic / Alignments /
  Taxonomy / Files / Run details), the run states (failed / running /
  storage-locked / no-result-files), and the streamed-through-API
  download flow. Screenshot target wired to `results-desktop.png`.
- `docs/user-guide/terminal.md` — full rewrite covering when to use
  the browser terminal, the ticket + WebSocket connection flow, the
  cockpit / manual side panel, `az login` sign-in for Azure CLI
  commands, safety rules, and a troubleshooting table. Screenshot
  target wired to `terminal-desktop.png`.
- `docs/user-guide/dashboard.md` — quality refresh to match the
  rewritten siblings. Adds workspace-selector (subscription / Workload
  RG / auto-refresh chip) walk-through, first-time setup flow
  (Workspace Picker / Setup Wizard / Getting Started panel), Sidecar
  Runtime section listing all six sidecars (`frontend`, `api`,
  `worker`, `beat`, `redis`, `terminal`) and their refresh model, a
  card-state table covering `healthy / loading / degraded /
  unavailable / network_blocked`, and "What's next" links into the
  other user-guide pages. Existing screenshots
  (`dashboard-overview-desktop.png`, `create-aks-cluster.png`,
  `get-database.png`) are reused; `dashboard-mobile.png` was
  re-captured at a true 390-px-wide mobile layout (single-column
  cards, Sidecar Runtime band hidden via `.dashboard-hide-mobile`)
  to replace the earlier all-`Loading` capture.

The API Reference page already matched the live UI and was not
touched.

## API / IaC diff summary

No backend, frontend, or infra code changed in this commit. Only
`docs/user-guide/*.md` and this change note.

## Validation evidence

- `uv run mkdocs build --strict` → clean build in ~4.4 s with no
  warnings.
- Per-page H1 audit on the built site:

  ```text
  site/user-guide/index.html         H1 count: 1
  site/user-guide/dashboard/         H1 count: 1
  site/user-guide/new-search/        H1 count: 1
  site/user-guide/jobs/              H1 count: 1
  site/user-guide/results/           H1 count: 1
  site/user-guide/terminal/          H1 count: 1
  ```

  All previously-broken pages now render a single `User Guide` /
  `New Search` / `Recent Searches` / `Results` / `Browser Terminal`
  title block instead of two stacked stubs.

## Follow-up (owner: human operator)

All four new screenshots referenced by the rewritten pages have been
captured and dropped into `docs/images/screenshots/`:

- `new-search-desktop.png` — `/blast/submit` page with 7-step
  stepper, FASTA editor, and command-preview rail.
- `jobs-desktop.png` — `/blast/jobs` Recent searches list with
  status chips, search box, and date-grouped rows.
- `results-desktop.png` — `/blast/jobs/<jobId>` Descriptions tab
  with hit table.
- `terminal-desktop.png` — connected `/terminal` shell with the
  Cockpit / Manual side panel; UPN tail masked per the redaction
  rules.

Capture targets follow the redaction rules in
[`docs/contributor-guide/screenshot-workflow.md`](../../contributor-guide/screenshot-workflow.md).
The UI Preview (`/elb-dashboard/mock-app/`) is an acceptable capture
source when a live demo workspace is not available.
