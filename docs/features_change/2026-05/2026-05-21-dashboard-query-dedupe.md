# Dashboard Query Dedupe

## Motivation

The dashboard rendered multiple BLAST-aware surfaces that queried the same `/api/blast/jobs` and `/api/monitor/metrics` data under different TanStack Query keys. That made the browser and API do duplicate polling while a BLAST submit was already putting pressure on the control plane.

## User-facing change

Dashboard BLAST status should update with less background API traffic. The AKS pulse row now shares the same cached BLAST job list and BLAST request-metrics query used by the other dashboard surfaces.

## API/IaC diff summary

- Reused the `blast-jobs` query key for ClusterPulse job polling.
- Reused the `request-metrics-blast` query key for ClusterPulse BLAST request metrics.
- Removed the obsolete `blast-jobs-for-pulse` cache invalidation.
- Skipped AKS cluster discovery in `useScopedBlastJobs` when a caller already has a cluster name.
- No backend route or IaC changes.

## Validation evidence

- `cd web && npm run build`: passed.
- `uv run pytest -q api/tests/test_blast_tasks.py`: 98 passed.
- `uv run ruff check api/tasks/blast/__init__.py api/tests/test_blast_tasks.py`: passed.
