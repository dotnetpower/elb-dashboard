# Terminal Cockpit functional fixes (20-item critique)

## Motivation

A critique of the Terminal Cockpit found that many panels rendered but did not
actually work as the labels implied. The command classifier produced false
positives, Session Chapters were 100% static (always "Authenticate = active"),
Insert always auto-ran the command with no review path, all cockpit state was
lost on side-panel toggle or reload, BLAST triage silently dropped malformed
input, and computed "near-tie" hits were never shown. This change derives 20
concrete improvements from that critique and implements them.

## User-facing change

**Command classifier (A1-A5)**

- A1 — Compound commands (`a && b`, `a | b`, `a; b`) are now classified by their
  **worst** segment, so a destructive tail is no longer hidden behind a benign head.
- A2 — Fixed the output-redirect false positive: `2>&1` / `&>` / fd-dups are no
  longer mistaken for a local file write; a genuine `> file` still is.
- A3 — Shell builtins and no-ops (`echo`, `cd`, `clear`, `history`, …) are now
  recognised as low-risk local reads instead of falling through to "unknown".
- A4 — Each classification carries a `confidence` (`high` / `medium` / `low`);
  the preview shows a confidence chip. Compound commands are capped at `medium`.
- A5 — Refined Kubernetes read recognition (`get`/`describe`/`logs`/`top`…) so
  read-only kubectl is not flagged at the same risk as mutating kubectl.

**Insert / Copy UX (B6-B9)**

- B6 — Copy buttons give visual feedback ("Copied" for ~1.8 s).
- B7 — New "run on insert" toggle; when off, the command is typed into the
  terminal for review instead of auto-running.
- B8 — Ctrl/Cmd+Enter in the preview inserts the command.
- B9 — When Insert is blocked, the specific reason is shown inline.

**Persistence (C10-C12)**

- The command draft, diagnostic context, and BLAST TSV now persist in
  `sessionStorage`, so toggling the side panel or reloading no longer wipes work.

**Session Chapters (D13)**

- The chapter ladder is now derived from real signals (Azure sign-in, the
  commands actually inserted, and whether triage produced evidence). The first
  unsatisfied chapter is "active"; completed chapters are "ready".

**BLAST triage (E14-E20)**

- E14 — Triage counts malformed outfmt-6 lines (`ignoredLineCount`) and surfaces
  a warning instead of dropping them silently.
- E15 — Near-tie ("ambiguous") top hits are now rendered.
- E16 — Triage panel gained a line counter and a Clear button.
- E17 — Diagnostic preset / recommendation / workflow clicks load the command
  into the preview and move focus there.
- E18 — The "Safer" button shows its target command and loads it (with focus).
- E19 — The az-login refresh control is now an accessible button (`aria-busy`,
  class-based styling instead of inline styles).
- E20 — The preview announces the current risk verdict via an `aria-live` region.

## API / IaC diff summary

No backend or infra changes. Frontend only:

- `web/src/pages/terminal/terminalCockpitModel.ts` — `CommandConfidence` type +
  `confidence` field on `CommandAnalysis`; compound worst-segment classifier
  (`classifySegment`); redirect/no-op/kubectl-read pattern refinement;
  `SessionChapterSignals`, `deriveSessionChapters`, `deriveChapterSignalsFromActivity`.
- `web/src/pages/terminal/terminalDiagnosticModel.ts` — `ignoredLineCount` field
  on `BlastTriage` + `countIgnoredOutfmt6Lines` helper, wired into both triage
  return sites.
- `web/src/pages/terminal/TerminalCockpit.tsx` — `onInsertCommand` gains an
  optional `{ run?: boolean }`; sessionStorage persistence; copy feedback;
  run-on-insert toggle; Ctrl/Cmd+Enter insert; inline block reason; live
  chapters; ambiguous-hit + ignored-line rendering; Clear button; preset focus;
  accessible az-refresh button; aria-live risk announcement.
- `web/src/pages/RemoteTerminal.tsx` — `handleInsertCommand` accepts
  `{ run?: boolean }` and only appends Enter when `run !== false` (default true,
  backward compatible).
- `web/src/theme/glass.css` — styles for confidence chips, sr-only region,
  run-on-insert toggle, inline block reason, accessible refresh button, and the
  near-tie list.

## Validation evidence

- `cd web && npm run build` — green (built in 8.43s).
- `cd web && npm test -- --run` — 68 files / 590 tests passing, including new
  tests for compound classification, confidence, redirect false-positive, no-op
  recognition, `deriveSessionChapters` / `deriveChapterSignalsFromActivity`, and
  `countIgnoredOutfmt6Lines` / `ignoredLineCount`.
- `npx eslint` on all changed files — no findings.
