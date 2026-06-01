---
title: "SRP split of k8s/monitoring.py into blast_status + warmup_status"
date: 2026-06-01
tags:
  - maintainability
  - kubernetes
---

# SRP split of `api/services/k8s/monitoring.py`

## Motivation

`api/services/k8s/monitoring.py` had grown to ~1161 lines and carried three
unrelated responsibility families behind one module: the Kubernetes session /
credential core, ElasticBLAST **search job status & cancellation**, and
ElasticBLAST **database warmup inspection**. Charter §11's SRP gate (a module's
`Responsibility` line must not need "and" chains across architectural concerns)
was violated — the single file owned blast-status caching, warmup job
reconciliation, and the shared session plumbing at once.

This is the previously-deferred item #2 from the maintainability review. The
deferral rationale was the wide monkeypatch-by-name surface (tests patch
`_get_k8s_session` / `_namespace_or_default` on the `monitoring` module). That
concern is resolved by following the existing `observability.py` precedent:
sibling modules resolve the patched seams **at call time** via a lazy
`from api.services.k8s.monitoring import _get_k8s_session` inside each function
body, so `monkeypatch.setattr(km, "_get_k8s_session", ...)` is still honoured.

## User-facing change

None. This is an internal refactor with zero runtime behaviour change. Every
public symbol previously importable from `api.services.k8s.monitoring`
(`k8s_check_blast_status`, `k8s_cancel_blast_job`, `k8s_warmup_status`,
`k8s_check_namespace_exists`, `k8s_release_warmup_cache`,
`k8s_release_stale_warmup_jobs`) remains importable from the same path via
re-export, so no caller — route, task, or `api.services.monitoring` facade — has
to change.

## API / IaC diff summary

- **New** [api/services/k8s/blast_status.py](../../../api/services/k8s/blast_status.py)
  — ElasticBLAST search job status and cancellation: `k8s_check_blast_status`,
  `k8s_cancel_blast_job`, the 3 s status cache (`_BLAST_STATUS_CACHE*`,
  `_reset_blast_status_cache`, `_fetch_blast_pods_and_jobs`), and the pure
  helpers (`_pod_has_env_value`, `_owned_job_names`, `_job_has_label_value`,
  `_container_terminated_state`).
- **New** [api/services/k8s/warmup_status.py](../../../api/services/k8s/warmup_status.py)
  — ElasticBLAST warmup state inspection: `k8s_warmup_status`,
  `k8s_check_namespace_exists`, `k8s_release_warmup_cache`,
  `k8s_release_stale_warmup_jobs`, and their helpers
  (`_database_status_from_setup_jobs`, `_merge_database_statuses`,
  `_mark_stale_warmup_nodes`, `_warmup_pods_and_logs`,
  `_append_warmup_daemonsets`, `_warmup_db_label_value`).
- **Changed** [api/services/k8s/monitoring.py](../../../api/services/k8s/monitoring.py)
  — keeps the session/credential core (`_get_k8s_session`,
  `_get_k8s_credential_material`, `reset_k8s_credential_cache`,
  `_namespace_or_default`) and the generic getters (`k8s_get_service_ip`,
  `k8s_get_deployment_ready_replicas`, `k8s_get_deployment_env_value`,
  `k8s_get_pods`). The two new modules are re-exported at the top so the
  module's public surface (`__all__`) is unchanged. Net ~−575 lines.

`_namespace_or_default` intentionally stays in `monitoring.py` (shared by both
new modules and patched by name in tests); both new modules lazy-import it from
`monitoring` inside the functions that need it. `_K8S_LABEL_VALUE_RE` is defined
locally in each new module to avoid module-load-time coupling.

## Validation evidence

- `uv run ruff check api` → **All checks passed!**
- Targeted suites (status/cancel/warmup seams + openapi proxy/TLS hooks):
  `uv run pytest -q api/tests/test_k8s_blast_status.py
  api/tests/test_blast_tasks.py api/tests/test_local_to_blast_job.py
  api/tests/test_warmup_route.py api/tests/test_openapi_proxy_route.py
  api/tests/test_openapi_tls_hook.py api/tests/test_openapi_pls_status.py
  api/tests/test_k8s_release_stale_warmup_jobs.py
  api/tests/test_k8s_warmup_status_parallel.py api/tests/test_k8s_list_events.py`
  → **216 passed**.
- Full sweep `uv run pytest -q api/tests` → **2378 passed, 3 skipped** (one
  unrelated flaky `test_terminal_exec.py::test_run_truncates_stdout_above_cap`
  subprocess-timeout under load, confirmed green in isolation).
- Consumer grep confirmed no external module imports the moved private helpers
  from `monitoring`; the only references are inside the new sibling modules and
  the re-exported `_reset_blast_status_cache`.
