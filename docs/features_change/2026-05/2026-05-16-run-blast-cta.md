# Run BLAST CTA

## Motivation

The New Search submit button still looked like a large gradient CTA after the page moved to a compact JetBrains Mono dashboard style. Its disabled state was also just a faded primary button, which made readiness less clear.

## User-facing change

The submit button now reads `Run BLAST`, uses a compact operational button style, and shows a muted disabled state until the form is ready. While submitting, the label changes to `Submitting`.

## API/IaC diff summary

- Frontend-only style and copy change for the New Search submit footer.
- No API or IaC changes.

## Validation evidence

- `cd web && npm run build` -> built successfully.
- Browser verification: disabled submit CTA renders as `Run BLAST` with muted outline styling and JetBrains Mono typography.
- Screenshot captured for the New Search submit bar.