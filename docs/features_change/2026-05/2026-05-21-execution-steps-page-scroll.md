# 2026-05-21 — Execution Steps: GitHub Actions-style scroll behaviour

## Motivation

While a BLAST job runs (`/blast/jobs/<job_id>` → `Execution Steps` card), each
step pane embedded **mini scroll boxes** inside the page:

- `HighlightedINI` / `HighlightedFASTA` / generic `<pre>` in
  `BlastFilePreview.tsx` clamped content at `180 – 220 px` with `overflowY:
  auto`, so `input.fa` and `elastic-blast.ini` previews showed inside a
  tiny scrollbar even though the backend already byte-capped the payload.
- The live K8s log stream was sliced to the **last 80 lines per phase**
  in `StepLogSection.tsx`, hiding the bulk of the run output.

The combined effect was that opening a step felt cramped — the user had
to scrub three nested scrollbars per step instead of just scrolling the
page like GitHub Actions does.

## User-facing change

- Each step's expanded content (`input.fa` preview, `elastic-blast.ini`
  preview, generic file previews) now flows inline. The page scroll is
  the only scroll. Backend `maxBytes` (`1000` / `10000`) keeps the
  rendered height bounded by design.
- Live log buffer per phase raised from **80 → 2000 lines**. If a phase
  exceeds the cap, a single head marker
  `[… 1,234 older lines trimmed]` is prepended so the user knows older
  output existed; the most recent 2000 lines are kept.
- Step collapse/expand semantics are unchanged: active and error steps
  auto-expand, completed / skipped / pending stay collapsed by default,
  and the user's explicit toggles are respected across step transitions.

## API / IaC diff summary

None — pure frontend change.

| File | Change |
|------|--------|
| `web/src/components/BlastFilePreview.tsx` | Drop `maxHeight` + `overflowY: "auto"` from `HighlightedINI`, `HighlightedFASTA`, and the generic `<pre>` block; restate intent in a short header comment. |
| `web/src/components/BlastStepTimeline/StepLogSection.tsx` | Replace the per-event `.slice(-80)` with a post-pass that keeps the last 2000 lines per phase and prepends a single trim marker when older lines were dropped. |

## Validation evidence

- `cd web && npm run build` — clean, 10.3 s, no TypeScript errors.
- Manual: open `/blast/jobs/<id>` for a running search; the `preparing`
  step shows the full input.fa preview without an internal scrollbar,
  `configuring` shows the full INI inline, and `running` retains a long
  tail of K8s log lines. When the cap is hit, the head marker appears.
