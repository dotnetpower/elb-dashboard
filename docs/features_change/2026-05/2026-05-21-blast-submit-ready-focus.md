# 2026-05-21 — `/blast/submit` ready-state focus pass

## Motivation

Two small UX papercuts on the New Search page surfaced during a walk-through:

1. Selecting an example sequence in the **Load example** modal fired a
   semantically empty toast (`Example loaded — MPXV F3L - NC_003310.1`).
   The textarea content already changes, the program tab flips to `blastn`,
   and the modal closes — the toast adds noise without information.
2. When the user fills in the last required field and validation finally
   passes, the **Run BLAST** button sits quietly at the bottom of the rail.
   There is no visual handoff telling the user "you're done; press this".

## User-facing change

- The "Example loaded — …" toast is gone. The modal close + new FASTA in the
  textarea is the only feedback now.
- On the false → true transition of `canSubmit`, the **Run BLAST** button
  emits a one-shot accent-tinted pulse (two cycles, ~1.4 s total) so it
  catches the eye. The pulse is suppressed under `prefers-reduced-motion`.
- The same transition also moves keyboard focus onto the **Run BLAST**
  button so `Enter` submits, **but only when the user is not actively
  typing**: if `document.activeElement` is an `<input>`, `<textarea>`,
  `<select>`, or a `contentEditable` element, the focus jump is skipped
  and only the pulse plays. This avoids stealing the caret mid-typing
  (e.g. while still entering the job title).
- The hidden mobile-footer variant and the visible desktop rail variant
  carry the same logic; only the button whose layout is currently visible
  (`offsetParent !== null`) is focused.

## API / IaC diff summary

None — pure frontend / styling change.

| File | Change |
|------|--------|
| `web/src/pages/blastSubmit/QuerySection.tsx` | Drop the `toast("Example loaded — …")` call from `loadExample`. |
| `web/src/pages/blastSubmit/SubmitSummaryRail.tsx` | Add `runBtnRef`, `wasReadyRef`, `readyPulse` state, and a transition-edge `useEffect` that focuses the Run BLAST button (when no text input is active) and toggles a one-shot CSS class. |
| `web/src/pages/blastSubmit/BlastSubmitFooter.tsx` | Mirror the same logic on the mobile footer variant. |
| `web/src/theme/glass.css` | Add `@keyframes blast-submit-ready-pulse` + `.blast-submit-btn--ready-pulse` modifier; respect `prefers-reduced-motion: reduce`. |

## Validation evidence

- `cd web && npm run build` — clean, 6.68 s, no TypeScript errors.
- Manual: open `/blast/submit`, fill required fields. The Run BLAST button
  pulses on the transition into the ready state; focus jumps to it only when
  the previously-active element is not a text input. Loading an example via
  the modal no longer raises a toast.
