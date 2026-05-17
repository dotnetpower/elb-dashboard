# Monitor Query Key Dedupe

## Motivation

The dashboard HTTP inspector showed several monitor endpoints taking around 1-3 seconds, especially ARM/Kubernetes-backed endpoints. Auth-layer caching was already implemented and validated, so the next low-risk performance issue was duplicate React Query cache keys for the same AKS/ACR resources.

## User-facing change

Dashboard readiness checks, cluster readiness gates, and submit-page cluster selection now share the same React Query cache entries as the main dashboard cards. This reduces duplicate `/api/monitor/aks` and `/api/monitor/acr` HTTP requests during initial dashboard load and navigation.

Kubernetes-backed monitor endpoints also reuse short-lived AKS kube credential material in process, so they no longer pay the ARM `list_cluster_*_credentials` cost on every poll. The BLAST Jobs list now gathers split child rows with one owner-scoped Table query instead of one query per parent job.

## API/IaC diff summary

- No API contract changes.
- No IaC changes.
- Frontend-only query-key alignment:
  - AKS queries use `['aks', subscriptionId, resourceGroup]`.
  - ACR readiness checks use `['acr', subscriptionId, resourceGroup, registryName]`.
- Backend performance improvements:
  - Cache parsed AKS kube credential material for 5 minutes in `api.services.k8s_monitoring` while still making fresh Kubernetes API reads per request.
  - Add `JobStateRepository.list_children_for_owner()` and use it in `/api/blast/jobs` to remove split-child N+1 Table queries.

## Validation evidence

- `uv run pytest -q api/tests/test_auth_caching.py` -> 9 passed.
- Local API log aggregate before the frontend dedupe showed `/api/monitor/aks` p50 around 939 ms and `/api/monitor/acr` p50 around 2627 ms, confirming the slow paths are downstream monitor calls rather than cached auth.
- Cached auth hot-path microbenchmark: 100,000 `_validate_token()` cache hits completed in 0.091639 s, about 0.000916 ms per call.
- Direct profiling found `aks.list_by_resource_group` at 2257 ms, `_get_k8s_session` at 1114 ms, and the actual Kubernetes `GET /api/v1/nodes` at 103 ms. This isolates K8s monitor slowness to repeated ARM credential discovery rather than Kubernetes itself.
- Direct Jobs profiling found `repo.list_for_owner(limit=50)` at 2050 ms and four sequential split-child lookups totalling 3482 ms before the batch lookup change.