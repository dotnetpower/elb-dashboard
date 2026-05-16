# New Search Mono Style

## Motivation

New Search used a softer default UI treatment than the dashboard and only applied JetBrains Mono to the FASTA text area. The submit workflow should feel like the same operational control-plane surface.

## User-facing change

The New Search page now uses JetBrains Mono typography within the submit surface, a compact dashboard-style header, tighter section spacing, squared step badges, and a product-specific title: `ElasticBLAST New Search`.

## API/IaC diff summary

- Frontend-only style and copy change for the BLAST submit page.
- No API or IaC changes.

## Validation evidence

- `cd web && npm run build` -> built successfully.
- Browser verification: `.blast-page`, header, section titles, controls, and FASTA text area render with `JetBrains Mono`.
- Screenshot captured for the New Search page after styling.