# BLAST Submit Proposal B Light Alignment

## Motivation

The light theme implementation of the BLAST submit Proposal B layout did not visually match the approved mockup. The structure was present, but card spacing, font stack, program tab styling, textarea sizing, disabled button color, and completed-step badges still followed the older glass UI defaults.

## User-facing change

The `/blast/submit` light theme now follows the Proposal B mockup more closely:

- The submit grid uses the mockup's `250px / 1fr / 320px` columns, `18px` gap, and `22px 28px` inner spacing.
- Form cards use white paper surfaces, `6px` corners, `18px 22px` padding, no glass shadow, and Microsoft Blue/Purple top stripes.
- Program tabs render as five independent card buttons with concise direction labels such as `Nucl -> Nucl` and `Protein -> tNucl`.
- The query textarea uses a white `140px` mono field like the mockup instead of the taller glass field.
- Completed/default sections show green step badges, and the disabled Run button uses the mockup's cool-gray disabled treatment.
- Secondary light surfaces that previously inherited `rgb(232, 230, 223)` now use white so program tabs, readiness strips, inputs, and the summary runbar match the paper-white Proposal B direction.
- The retired light palette hook was removed so Aurora/MS palette state no longer lingers in `web/src`.

Dark theme styling remains isolated from these light-only overrides.

## API/IaC diff summary

No API or infrastructure changes.

Frontend-only changes:

- `web/src/theme/blast-submit-layout.css` now owns Proposal B light-mode visual alignment.
- `web/src/pages/blastSubmit/ProgramSection.tsx` uses concise program direction labels for tab cards.
- Completed/default section markers were added to submit sections so badges match the mockup state.
- `web/src/hooks/usePalette.ts` was deleted because the palette switcher was already removed from the UI.

Hardening pass:

- Narrowed Proposal B card styling from a broad `.bsl-grid .glass-card` selector to `.bsl-grid .blast-section` so nested dialogs or future rail/preflight cards are not restyled accidentally.
- Narrowed Proposal B light font overrides from the whole `.blast-page` subtree to `.bsl-grid` controls only.
- Added explicit `type="button"` to Proposal B stepper and program tab buttons.
- Kept full program descriptions available via button `title` after shortening the visible labels.
- Moved summary rail and mobile footer inline styles into CSS classes, removed redundant `aria-disabled` from native submit buttons, and gave required-field links descriptive `aria-label` values.
- Added max-height/scroll handling to the sticky stepper and documented the Proposal B sticky rail z-index band.
- Hardened the app topbar at mid-width desktop sizes by switching the dense navigation row into the existing hamburger drawer and tightening the latest-job chip.
- Normalized active topbar navigation items to a full rounded pill so Dashboard no longer reads as a different left-border tab shape.

## Validation evidence

- `cd /home/moonchoi/dev/elb-dashboard/web && npx tsc --noEmit`
- `cd /home/moonchoi/dev/elb-dashboard/web && npm run build` succeeded; Vite reported only the existing chunk-size warning.
- Playwright light-mode computed style check confirmed:
  - first submit card: white background, `3px solid #0078d4` top border, `6px` radius, `18px 22px` padding
  - program tab: `95px x 81px`, `type="button"`, and full description in `title`
  - query textarea: `140px` height and mono font
  - disabled Run buttons: no redundant `aria-disabled` in either rail or mobile footer buttons
  - current viewport: `scrollWidth 1254 <= innerWidth 1264`; hamburger drawer trigger visible and nav hidden off-canvas
- Playwright dark-mode computed style check confirmed dark cards still use the existing dark surface, `8px` radius, and legacy dark textarea sizing.
- Playwright modal hardening check confirmed the taxonomy dialog renders above the stepper/summary rail (`dialog z-index 1000`, rail/stepper `z-index 5`) and focuses the search input.
- Playwright selector hardening check confirmed no `.bsl-grid .glass-card` rule remains in loaded stylesheets.
