# Submit Pipeline Empty State

## Motivation

When there are no BLAST submit requests, the dashboard should say that clearly instead of rendering a broken-looking sparkline or hidden counts. This is especially visible in local development when the job state store is not configured and the jobs endpoint returns an empty list with degraded metadata.

## User-Facing Change

The AKS Bento Submit Pipeline card now prioritizes an explicit empty state when the available job sources return zero submit requests. The graph and detailed submit-rate view remain reserved for clusters with at least one submit request.

## API / IaC Diff Summary

- Frontend only: adjusted the `ClusterBento` submit-pipeline rendering branch.
- No API or IaC changes.

## Validation Evidence

- `npm run build` in `web/` passed.
- Browser check at `http://127.0.0.1:8090/` showed `No submit requests yet. Start a BLAST run to populate this card.` and did not render the submit sparkline when `/api/blast/jobs` returned an empty jobs list.