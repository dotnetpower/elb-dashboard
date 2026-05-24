# BLAST Database Large Download Focus

## Motivation

Large BLAST database confirmation appears near the bottom of the database modal. Operators could miss the final action because the modal scroll position did not reliably bring the confirmation block and `Start Download` control into view.

## User-facing change

When an operator clicks `Get` on a large database, the confirmation panel now scrolls into view and moves keyboard focus to the emphasized `Start Download` button. The button uses a stronger warning-colored treatment so the next required action is visually clear.

## API / IaC diff summary

No API or infrastructure changes. The change is limited to the React confirmation component and its component-scoped CSS.

## Validation evidence

- `npm run build` in `web/`
- Browser check with [Playwright](https://playwright.dev/): clicking `Get` for `core_nt` scrolled the modal to the confirmation panel and made `Start Download` the active element.