# AKS SKU Warning Noise

## Motivation

The dashboard calls `/api/aks/skus` during normal setup and dashboard rendering. The route now returns a valid static ElasticBLAST-compatible SKU catalog, but it still emitted the generic `STUB_CALLED` warning on every request. That made local hardening logs noisier than needed while debugging BLAST submit bottlenecks.

## User-facing change

Dashboard/API logs should be quieter. Normal AKS SKU catalog requests no longer appear as warning-level stub calls.

## API/IaC diff summary

- Removed `_stub_log("aks/skus", ...)` from the AKS SKU route.
- No response shape, frontend, or IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_skus.py api/tests/test_route_contracts.py api/tests/test_blast_tasks.py`: 115 passed.
- `uv run ruff check api/routes/aks/skus.py api/tasks/blast/__init__.py api/tests/test_blast_tasks.py`: passed.
