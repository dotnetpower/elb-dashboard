# Kubernetes Observability SRP

## Motivation

`api.services.k8s_monitoring` still mixed Kubernetes credential/session ownership with metrics, pod logs, and event normalization. Pod logs and events are read-only observability helpers with a smaller responsibility boundary than the broader monitoring module.

## User-Facing Change

- No intentional API or UI behavior change.
- Existing monitor routes continue to call `k8s_pod_logs` and `k8s_list_events` through the same compatibility surfaces.

## API / IaC Diff Summary

- Split pod log and event helpers into `api.services.k8s_observability`.
- Kept compatibility imports from `api.services.k8s_monitoring` and `api.services.monitoring`.
- Preserved server-side Kubernetes session creation in `api.services.k8s_monitoring`.
- No IaC changes.

## Validation Evidence

- Focused k8s/smoke tests: `uv run pytest -q api/tests/test_k8s_list_events.py api/tests/test_smoke.py api/tests/test_k8s_blast_status.py` -> 66 passed.
- Backend lint: `uv run ruff check api/services/k8s_monitoring.py api/services/k8s_observability.py api/services/monitoring.py` -> passed.
- Full backend regression: `uv run pytest -q api/tests` -> 786 passed.
- VS Code diagnostics on changed Python modules -> no errors.