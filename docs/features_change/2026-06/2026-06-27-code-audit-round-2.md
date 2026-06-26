---
title: Code audit round 2 — WS frame cap, SSE drop observability, deploy override audit, K8s log socket release
description: Second autonomous code-audit pass. Five real fixes shipped (WebSocket frame size cap with env override, drop-oldest observability on three SSE fan-outs, deploy override audit trail, K8s log socket release, reconcile state-machine consistency for terminal flips). Three verified false positives. One intentional OPEN follow-up left in cancel_task.py.
tags:
  - operate
  - blast
---

# Code audit round 2 — second pass after the first round shipped

## Motivation

Round 1 (`cf2d0f9`) closed 16 real issues out of 28 candidates. This round
ran another exploratory sweep across the same surfaces and surfaced **32
candidate issues**. After verifying each against the actual source, **3
were verified false positives** (already handled by prior shipped code),
**1 was an intentional OPEN follow-up** (cancel-specific lifecycle in
`cancel_task.py:92`), and **5 were real** — all low-risk slices, no new
public contract.

## Fixes shipped

### F2 — WebSocket frame size cap with env override

`websockets.connect(..., max_size=None)` in
[api/routes/terminal/ws.py](../../../api/routes/terminal/ws.py) accepted
unbounded frames from the upstream `ttyd` loopback. A malformed terminal
session (or a hostile process inside the terminal sidecar writing
megabytes of escape sequences) could OOM the `api` sidecar.

New helper `_ws_max_message_bytes()` reads `TERMINAL_WS_MAX_MESSAGE_BYTES`
(default **8 MiB**; `0` keeps the unbounded legacy behaviour for genuine
file-paste sessions). Replaces the `max_size=None` literal so the cap
applies to both upstream → browser and browser → upstream frames.

### F3 — Drop-oldest observability on three SSE fan-outs

When a slow SSE subscriber backed up the per-subscriber queue, the
producer silently discarded the stalest event to keep the live tail
honest. There was no log line, so a production incident where the
dashboard appears stuck (events dropped, never recovered for that
client) was invisible until someone read the source.

Added one `LOGGER.info(...)` line at the discard site in:

* [api/services/jobs_events_bus.py](../../../api/services/jobs_events_bus.py)
  `_offer()` coalesce branch — `jobs_events_bus drop-oldest on overflow`
* [api/routes/monitor/sidecars.py](../../../api/routes/monitor/sidecars.py)
  `_SidecarBroadcaster._run()` `QueueFull` branch — `sidecar SSE drop-oldest on overflow`
* [api/routes/blast/logs.py](../../../api/routes/blast/logs.py)
  `enqueue_from_thread()` queue-full branch — `blast log SSE drop-oldest on overflow job_id=…`

INFO not WARN: drop-oldest is expected design behaviour when a
subscriber is genuinely slow, but the operator needs the line to
correlate a "dashboard frozen" complaint with the SSE pipeline.

### F4 — Deploy override audit trail

[scripts/dev/quick-deploy.sh](../../../scripts/dev/quick-deploy.sh) accepts
six override env vars that disable safety nets
(`ELB_SKIP_ACR_PRUNE`, `ELB_SKIP_WORKSPACE_TAGS`, `ELB_ALLOW_SUB_MISMATCH`,
`ELB_ALLOW_AUTH_BYPASS_IN_CLOUD`, `ELB_QUICK_DEPLOY_SKIP_CONFIRM`,
`ELB_SKIP_HOOKS`). A previous incident silently shipped to the wrong
subscription because the sub-mismatch guard was bypassed without it
showing up in the deploy log.

New `log_active_overrides()` function emits a single timestamped block
**after** `confirm_deploy_target` succeeds, listing every override that
is currently active. Called from both deploy paths (`api` / `frontend`).
No new behaviour — only an audit trail.

### F5 — K8s log socket release on early generator close

[api/services/job_logs/k8s.py](../../../api/services/job_logs/k8s.py)
`stream_k8s_log_lines()` opened a streaming `session.get(...)` and
relied on Python GC to close it when the generator was closed mid-stream
(browser disconnect, `stop_event`, follower task cancellation). Under
streaming load that leaks connections from the K8s API session pool.

Added a `try/finally` around the iter-lines loop that calls
`response.close()` if available (guard for test doubles that return a
plain object). Plus the outer `session.close()` in the existing
`finally` block. Socket is released whether the generator finishes
normally, raises, or is closed early.

### F7 — Reconcile state-machine consistency for terminal flips

[api/tasks/blast/reconcile_task.py](../../../api/tasks/blast/reconcile_task.py)
had three sites where the reconciler called `repo.update(status=...)`
directly. For **terminal** transitions (`completed`, `failed`,
`worker_lost`) that bypassed `_blast._update_state(...)`, which is the
only path that emits the `blast` customEvent, sweeps orphan `_progress`
steps left by a crashed worker, and triggers the artifact finalizer
once.

Switched the three **terminal flip** sites to go through
`_blast._update_state(...)` with an `event=` tag identifying the
reconcile pass. Non-terminal nudges (Celery SUCCESS with `status="running"`
+ phase update) keep the direct `repo.update(...)` — they don't need
state-machine ceremony, and the dashboard tests assert exact equality
on the call kwargs.

## False positives (verified, no code change)

* **F1 — `prepare_db_via_aks` cleanup leaks `update_in_progress=True`** —
  All 5 try/except sites in the orchestrator already call
  `_mark_partial(...)` which sets `update_in_progress=False`. Verified by
  reading the function end-to-end.
* **F6 — SSE client disconnect leaves a dangling subscriber** — The
  `finally:` block in the producer already calls `_remove_subscriber()`.
  Verified by tracing the lifecycle.
* **F8 — orphaned prepare-db rows are not healed across worker restart** —
  `reconcile_orphaned_prepare_db` already ships and runs on the beat
  schedule; the audit agent missed it.

## Intentional OPEN

* **`api/tasks/blast/cancel_task.py:92` direct `repo.update()` on
  cancel** — Cancel has a different lifecycle (immediate UI feedback
  required, no artifact finalizer, no progress sweep semantics). Left
  intentional. Documented in the source comment.

## Validation

* `uv run ruff check api` → All checks passed.
* Targeted: `uv run pytest api/tests/test_blast_tasks.py api/tests/test_job_log_k8s.py`
  → 162 passed.
* Full suite: `uv run pytest api/tests` — same as prior baseline modulo
  the two test-contract regressions caught and fixed during this
  session (non-terminal reconcile nudge reverted to direct
  `repo.update`; K8s log close guarded for plain-object test doubles).

## Risk

Low. Each fix is narrowly scoped:

* F2 ships with `TERMINAL_WS_MAX_MESSAGE_BYTES=0` as the unbounded escape
  hatch if any legitimate paste session needs >8 MiB.
* F3 is logging-only, no behaviour change.
* F4 is logging-only, no behaviour change.
* F5 the close() call is guarded with `callable()` so test doubles and
  any future `Response` subclass that doesn't ship `close()` continue
  to work.
* F7 terminal-flip funnel preserves the existing repo write; only adds
  the cross-cutting state-machine effects that were already in place
  for the submit/cancel paths.
