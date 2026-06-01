# Auto-stop livelock: act task skipped its own enqueued stop on cooldown

**Date:** 2026-06-01
**Area:** AKS idle auto-stop (`api/services/auto_stop_evaluator.py`, `api/tasks/azure/idle_autostop.py`)

## Motivation

A deployed, idle AKS cluster (`elb-cluster-02`) was never auto-stopped even
though auto-stop was enabled and the idle window had long elapsed. App Insights
showed the per-cluster act task repeating every cooldown window without ever
calling `stop_aks`:

```
2026-06-01T07:25:16 auto_stop_aks late-skip cluster=elb-cluster-02 reason=cooldown
2026-06-01T06:55:16 auto_stop_aks late-skip cluster=elb-cluster-02 reason=cooldown
```

### Root cause — a self-inflicted livelock

The auto-stop pipeline is two Celery tasks:

1. `evaluate_idle_clusters` (beat, every 300 s) — **decide**. For each enabled
   preference it runs `evaluate_cluster`; on a `stop` verdict it stamps
   `last_stop_at = now` as a *preflight double-enqueue guard* (so an overlapping
   beat tick sees `is_in_cooldown` and refuses to enqueue a second stop), then
   enqueues `auto_stop_aks`.
2. `auto_stop_aks` (per-cluster) — **act**. It re-runs `evaluate_cluster` to
   close the decide-vs-act race, then calls `stop_aks`.

The act task's re-evaluation saw the `last_stop_at` stamp the beat had *just*
written one moment earlier, so `is_in_cooldown` returned True and the act task
late-skipped with `reason=cooldown` — skipping the very stop it was enqueued to
perform. The next beat tick stamped again, the next act task skipped again, and
the cluster stayed running forever (the stamp keeps getting refreshed inside its
own 30-minute cooldown window).

## User-facing change

Idle clusters with auto-stop enabled now actually stop after the configured idle
window. No SPA/UI change; the cost-saver simply works as designed.

## Code change

* `evaluate_cluster(...)` gains an `ignore_cooldown: bool = False` parameter.
  When True, the cooldown gate is skipped. All other gates (enabled, ARM
  `power_state == "Running"`, extend, active-jobs, state-repo reachability,
  history-truncation, idle-window) are unchanged.
* `auto_stop_aks` calls `evaluate_cluster(..., ignore_cooldown=True)`. The
  cooldown concern belongs to the **decide** pass (beat) and the SPA countdown,
  not to the **act** pass — the act task only needs to re-confirm the
  race-sensitive gates. The beat `decide` call and the SPA status route keep the
  default `ignore_cooldown=False`, so a genuine recent stop still blocks an
  immediate re-stop.

## Validation

* `uv run pytest -q api/tests/test_auto_stop_task.py api/tests/test_auto_stop_evaluator.py` — 26 passed.
* New `test_ignore_cooldown_bypasses_cooldown_gate` (evaluator unit) proves the
  default path keeps on `cooldown` while `ignore_cooldown=True` proceeds to
  `stop` for the same idle, freshly-stamped preference.
* New `test_auto_stop_aks_stops_despite_preflight_cooldown_stamp` (task,
  end-to-end with the real `evaluate_cluster`) reproduces the prod livelock —
  a `last_stop_at` stamped 1 minute ago plus an idle cluster — and asserts the
  act task now calls `stop_aks` instead of late-skipping.
* `uv run ruff check` on all four touched files — clean.
* Full suite `uv run pytest -q api/tests` — 2381 passed (1 unrelated flaky
  `test_terminal_exec.py::test_run_truncates_stdout_above_cap` subprocess-timeout
  test, passes in isolation).
