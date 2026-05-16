# Jobs Mono Style

## Motivation

After the dashboard and New Search pages moved toward a compact JetBrains Mono control-plane style, the Jobs page still used the older generic page header and table treatment.

## User-facing change

The Jobs page now uses JetBrains Mono typography, a compact dashboard-style header titled `ElasticBLAST Jobs`, a framed filter bar, and framed job tables that match the operational surface used by New Search.

## API/IaC diff summary

- Frontend-only style and copy change for the Jobs page.
- No API or IaC changes.

## Validation evidence

- `cd web && npm run build` -> built successfully.
- Browser verification: Jobs header renders `ElasticBLAST Jobs` with JetBrains Mono typography and dashboard-style framing.
- Screenshot captured for the Jobs page empty/degraded state.
