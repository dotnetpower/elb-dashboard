# Monitor Backend Latency

## Motivation

Recent HTTP inspector samples showed monitor endpoints spending 1-3 seconds per request even after auth-layer caching. Direct profiling showed the slow paths were downstream Azure calls: AKS ARM reads, AKS kube credential retrieval, and Azure Table scans for split-child job summaries.

## User-facing change

Dashboard monitor refreshes should feel less bursty, especially cluster-detail Kubernetes panels and BLAST Jobs. Repeated K8s monitor calls no longer fetch AKS kube credential material from ARM on every request, and the jobs list no longer performs one child-summary table query per parent job.

## API/IaC diff summary

- No HTTP contract changes.
- No IaC changes.
- `api.services.k8s_monitoring` now caches AKS kube credential material for a short TTL (`K8S_CREDENTIAL_CACHE_TTL_SECONDS`, default 300 seconds, capped at 3600).
- `JobStateRepository.list_children_for_owner()` batches split-child job lookup by owner and filters the requested parent job ids in process.
- `/api/blast/jobs` uses the batched child-summary path when available, with the previous per-parent lookup kept as a compatibility fallback.

## Validation evidence

- Targeted regression tests were added for K8s credential cache reuse, owner-batched child grouping, and `/api/blast/jobs` batch summary usage.
- `uv run ruff check api/services/k8s_monitoring.py api/services/state_repo.py api/tests/test_k8s_list_events.py api/tests/test_local_to_blast_job.py api/tests/test_state_repo.py` -> passed.
- `uv run python -m py_compile api/routes/stubs.py` -> passed.
- `uv run pytest -q api/tests/test_k8s_list_events.py api/tests/test_local_to_blast_job.py api/tests/test_state_repo.py` -> 16 passed.