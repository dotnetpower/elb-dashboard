---
title: Auto-stop no longer stuck on "history_scan_truncated"
description: Remove the stale truncation guard that permanently disabled AKS auto-stop for any cluster with >= 200 historical job rows.
tags:
  - operate
  - blast
---

# Auto-stop no longer stuck on "history_scan_truncated"

## Motivation

A cluster with auto-stop enabled never stopped and the SPA banner showed
**"Auto-stop armed · Too many recent jobs to scan — staying running."**
(evaluator reason `history_scan_truncated`). Auto-stop was permanently
defeated for any cluster that had accumulated a normal amount of job history.

## Root cause

`api/services/auto_stop_evaluator.py::_scan_cluster_jobs` called
`repo.list_for_scope(limit=200)` and set `truncated = len(rows) >= 200`.
`evaluate_cluster` then returned `keep / history_scan_truncated` whenever the
scan was full, on the assumption that "Azure Tables is not timestamp-ordered,
so the true latest-activity row may lie beyond the window."

That assumption was stale. `list_for_scope` now routes through
`StateRepository._list_recent_sorted`, which reads the full filtered set (up to
the repo hard cap) and returns the genuinely **most-recent** `limit` rows
sorted by `created_at` descending. So the rows examined are already the newest
ones and the idle anchor (`latest`) is reliable — there is no "latest beyond the
window" hazard. The `truncated` flag therefore only meant "this cluster has
>= 200 historical rows", which is the steady state for any real cluster, and the
guard turned into a permanent "never stop".

## User-facing change

* Clusters with a large job history now auto-stop normally when idle.
* The `history_scan_truncated` keep verdict / banner reason is no longer
  produced by the evaluator. The defensive UI label and the route's
  non-cacheable-reason entry are retained as harmless fallbacks.

## API / IaC diff summary

* `api/services/auto_stop_evaluator.py`
  * `_scan_cluster_jobs` returns `(active_count, latest, ok)` (dropped the
    unused `truncated` element) and its docstring documents the
    sorted-read ordering guarantee.
  * `evaluate_cluster` no longer has the `if truncated:` →
    `keep / history_scan_truncated` branch.
* No IaC change.

## Why removing the guard is safe

* The idle anchor (`latest`) is computed from the genuinely newest rows, so the
  idle clock is accurate.
* Active jobs are authoritatively caught by the live K8s `app=blast` probe
  (`live_active_jobs`), which is folded into the keep decision independently of
  the Table scan window.
* Table-unreachable still fails safe via the unchanged `ok=False` →
  `keep / state_repo_unreachable` path.

## Validation evidence

* `uv run pytest -q api/tests/test_auto_stop_evaluator.py api/tests/test_auto_stop.py api/tests/test_auto_stop_task.py api/tests/test_aks_autostop_route.py api/tests/test_idle_autostop_sb_queue.py api/tests/test_tasks_facade_contract.py` → 157 passed.
* Repurposed regression `test_large_history_does_not_block_stop`: 200 terminal
  rows aged past the idle window now yield `verdict == "stop"` (previously
  `keep / history_scan_truncated`).
* `uv run ruff check api/services/auto_stop_evaluator.py api/tests/test_auto_stop_evaluator.py` → clean.
