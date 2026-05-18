# Taxonomy Modal Scroll Fallback

## Motivation

The taxonomy search modal could hide the Filter mode controls when the viewport was short or when modal content grew taller than the available screen space.

## User-facing change

The taxonomy modal now allows the modal body to scroll when needed. On narrow or short screens, the stacked modal content scrolls as one body so the Filter mode section and footer actions remain reachable.

## API/IaC diff summary

No API or IaC changes. Frontend CSS only.

## Validation evidence

Passed: `npm run build` in `web/`.
Passed: Playwright low-viewport check with a taxonomy modal fixture confirmed the modal body scrolls and the Filter mode section is reachable after scrolling.
