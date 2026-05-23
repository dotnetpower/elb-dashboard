# 2026-05-23 — services/k8s subpackage (metrics/monitoring/observability/timestamps)

## Motivation
Continue Phase A: fold the remaining flat `k8s_*.py` files into the existing
`services/k8s/` subpackage (which previously held only `client.py`, `nodes.py`).

## Diff
- `api/services/k8s_metrics.py` → `api/services/k8s/metrics.py`
- `api/services/k8s_monitoring.py` (1277 LOC) → `api/services/k8s/monitoring.py`
- `api/services/k8s_observability.py` → `api/services/k8s/observability.py`
- `api/services/k8s_timestamps.py` → `api/services/k8s/timestamps.py`
- New shims at legacy paths use a module-level `__getattr__` proxy (not explicit
  `__all__`) because the 30+ public + private symbols in `k8s.monitoring` make
  an explicit list a maintenance burden; the proxy forwards everything to the
  real module.
- All in-repo callers, internal cross-imports inside the moved files, and
  monkey-patch strings updated.

## Validation
- `uv run pytest -q api/tests` → 1260 passed
- `uv run ruff check api` → All checks passed
