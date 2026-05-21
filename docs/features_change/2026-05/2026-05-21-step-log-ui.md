# 2026-05-21 — Step log UI: no scrollbar, full output, CI-style highlighting

## Motivation

The BLAST step log block was a 600 px-tall scrollable widget with a
40-line collapse threshold and minimal syntax colouring. Users running
real `blastn` jobs reported that (a) they had to scroll inside a tiny
inner pane to see the rest of the log, and (b) the colouring was hard
to read — only one shade per category and no separation of timestamps
from content.

## User-facing change

The step log block now renders **all lines inline** — no inner
scrollbar, no "Show all N lines" button, no fold. Each line is rendered
with line number + ISO timestamp prefix (dimmed) + CI-style coloured
body:

- `error` / `Traceback` / `panic:` / `<Error>` / `ContainerNotFound` — red with subtle red wash
- `WARN` / `WARNING` / `⚠` / `deprecated` — amber
- `✓` / `EXIT_CODE=0` / `SUCCESS` / `Completed` — green
- `---` / `===` / `###` section banners — accent (cyan-violet)
- `$ ` / `> ` / `# ` command lines — light cyan
- `INFO:` / `DEBUG` / `TRACE` — faint grey
- `BLAST RUNTIME` / `Database:` / `azcopy …` / `elastic-blast …` /
  `kubectl …` / BLAST program invocations — soft violet
- Everything else — default muted text

ANSI escape codes (`\x1B[…m`) are stripped before classification so
azcopy / coloured pytest output no longer leaves `[0;31m` debris in the
DOM.

## API / IaC diff summary

- [web/src/components/BlastStepTimeline/StepLogBlock.tsx](../../../web/src/components/BlastStepTimeline/StepLogBlock.tsx)
  rewritten: removed `isExpanded` / `isLong` state and the
  "Show all N lines" / "Collapse" buttons; added `stripAnsi`,
  `classifyLineKind`, `tokeniseLine`; renders every detail line.
- [web/src/theme/glass.css](../../../web/src/theme/glass.css):
  - `.step-log-detail` no longer has `max-height` / `overflow-y`.
  - Removed `.step-log-detail--collapsed`, its `::after` fade, and
    `.step-log-expand`.
  - Added `.step-log-row`, `.step-log-ts`, `.step-log-text--cmd`,
    `.step-log-text--blast` plus matching light-theme overrides.
- No backend / Bicep changes.

## Validation

- `cd web && npm run build` → built clean (6.4 s).
- `cd web && npx eslint src/components/BlastStepTimeline` → clean.
- Manual screenshot: a sample 200-line BLAST run shows continuous
  output with no inner scroll and clear per-category colours.

## Out of scope

Per-token highlighting (numeric values, URLs, file paths within a line)
remains line-level for now. The token model is in place so a future
pass can extract URLs / numbers without restructuring the renderer.
