# API response contract loading skeleton

## Motivation

The API Reference page showed animated placeholders while discovering the OpenAPI service and loading the specification, but the API response contract panel rendered real content immediately and looked visually out of phase.

## User-facing change

The API response contract panel now uses the same animated skeleton treatment as the rest of the API Reference page during service discovery, image status loading, and specification loading.

## API/IaC diff summary

- Frontend: added a loading variant to the API response contract panel and wired it to the API Reference loading state.
- API: no changes.
- IaC: no changes.

## Validation evidence

- `cd /home/moonchoi/dev/elb-dashboard && npm --prefix web run build` passed.
- Browser check on `/docs` with delayed API discovery confirmed the response contract panel renders `aria-busy="true"` with 30 `.skeleton` nodes using the `skeleton-shimmer` animation.