# ElasticBLAST Dashboard Title

## Motivation

The dashboard page header repeated the generic `Dashboard` label immediately after the breadcrumb. The title should identify the product surface more clearly.

## User-facing change

The dashboard page header now reads `ElasticBLAST Dashboard`.

## API/IaC diff summary

- Frontend-only copy change in the dashboard page header.
- No API or IaC changes.

## Validation evidence

- `cd web && npm run build` -> built successfully.
- Browser verification: dashboard header renders `ElasticBLAST Dashboard`.