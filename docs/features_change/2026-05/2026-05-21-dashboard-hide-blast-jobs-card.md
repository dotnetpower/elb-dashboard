# Dashboard BLAST jobs card removal

## Motivation

The dashboard already surfaces job activity inside the cluster row and keeps the full job history on the Recent searches page. The separate bottom `BLAST Jobs` dashboard card duplicated that workflow and added visual noise.

## User-facing change

The dashboard no longer renders the bottom `BLAST jobs` section. Job monitoring remains available in the cluster row and under BLAST > Recent searches.

## API / IaC diff summary

No API or IaC changes. The frontend dashboard grid simply stops mounting `JobCard`.

## Validation evidence

- `cd web && npm run build`