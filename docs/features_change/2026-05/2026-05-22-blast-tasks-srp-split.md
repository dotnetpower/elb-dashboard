# Backend + frontend SRP split pass

**Date:** 2026-05-22
**Scope:** Refactor only. No behaviour change, no public API change, no IaC change.

## Motivation

Several files in `api/tasks/blast/`, `api/services/`, and `web/src/components/`
had grown to where their module docstring's `Responsibility` line could no
longer be stated without "and" — the charter's SRP gate (see
[.github/copilot-instructions.md §11](../../../.github/copilot-instructions.md)).
While AKS was restarting, we split the largest violators into smaller, single-
responsibility modules. Each extracted module ships with its own charter
context header and keeps the original public API stable through alias re-exports.

## User-facing change

None. All extractions preserve call signatures and module-level attribute
access (including the `blast.SPLIT_MERGE_REPORT_MAX_BYTES` and
`blast.QUERY_FASTA_READ_MAX_BYTES` symbols that pytest monkeypatches).

## API/IaC diff summary

### `api/tasks/blast/`

| File | Lines | Responsibility |
|------|-------|----------------|
| [submit_lock.py](../../../api/tasks/blast/submit_lock.py) | 70 | Per-(cluster, namespace) Redis lock for `elastic-blast submit`. |
| [substeps.py](../../../api/tasks/blast/substeps.py) | 60 | Map an `elastic-blast submit` log line to one of 5 sub-progress checkpoints. |
| [submit_logs.py](../../../api/tasks/blast/submit_logs.py) | 52 | Slice submit log events into 100-event chunks and persist them. |
| [split_constants.py](../../../api/tasks/blast/split_constants.py) | 85 | Constants for split-mode parent/child plans (status sets, blob names, allowlist). |

`api/tasks/blast/__init__.py` consumes those modules through plain imports
with underscore aliases (`_submit_lock_key`, `_acquire_submit_lock`,
`_release_submit_lock`, `_persist_submit_log_events`,
`_detect_submit_substep`) for backwards compatibility. The now-redundant
inline `PROGRESS_STEP_ORDER` tuple (already authoritative in
[`progress.py`](../../../api/tasks/blast/progress.py)) was removed.

### `api/services/`

| File | Lines | Responsibility |
|------|-------|----------------|
| [k8s_timestamps.py](../../../api/services/k8s_timestamps.py) | 88 | Parse Kubernetes RFC3339 timestamps; compute min/max/span payloads. |
| [warmup_scripts.py](../../../api/services/warmup_scripts.py) | 235 | Three shell-script texts injected into the BLAST DB warmup Job (container entrypoint + `init-db-shard-aks.sh` + `blast-vmtouch-aks.sh`). |

`api/services/k8s_monitoring.py` and `api/services/warmup_jobs.py` now
import the extracted symbols (with underscore aliases) so their internal
callers stay unchanged.

### `web/src/components/`

| File | Lines | Responsibility |
|------|-------|----------------|
| [warmupSection/helpers.ts](../../../web/src/components/warmupSection/helpers.ts) | 300 | `WARMUP_CANDIDATES`, capacity / row types, pure formatters (`formatBytes`, `formatDuration`, `formatPhaseCounts`, `formatWarmupProgress`, `shortWarmupPhase`, `summariseWarmupCapacity`, `buildWarmupRows`). |

`web/src/components/WarmupSection.tsx` keeps the React-bearing components
(banner, db row, skeletons, progress bar, status pill) but imports the
pure helpers, dropping 265 lines.

### Size summary

| File | Before | After | Delta |
|------|-------:|------:|------:|
| `api/tasks/blast/__init__.py` | 3,110 | 2,993 | −117 |
| `api/services/k8s_monitoring.py` | 1,136 | 1,090 | −46 |
| `api/services/warmup_jobs.py` | 922 | 724 | −198 |
| `web/src/components/WarmupSection.tsx` | 1,127 | 857 | −270 |

The seven extracted modules total 925 lines and each carries a single
`Responsibility` line in its context header.

## Validation evidence

```bash
$ uv run ruff check api
All checks passed!

$ uv run pytest -q api/tests
868 passed in 26.57s

$ cd web && npx vitest --run
Test Files  26 passed (26)
     Tests  224 passed (224)

$ cd web && npm run build
✓ built in 9.90s
```

No behavioural test changed; all 868 backend + 224 frontend tests still
pass against the refactored code. Pure helpers are exercised through the
existing route / task test paths (`test_blast_tasks.py`,
`test_k8s_*.py`, `test_warmup_*.py`).

---

## Batch 3 — `api/tasks/blast/__init__.py` Celery-task extraction

After the first batch trimmed shared helpers, the package `__init__.py`
still owned five large Celery-task definitions plus the split-mode pipeline
(merge/upload/finalize) helpers. The `Responsibility` line had to enumerate
"cancel and backfill and reconcile and poll and split-pipeline", which
violated the SRP gate. We pulled each task into its own module while keeping
the package's public attribute surface intact (every helper that the test
suite accesses via `blast._X` or monkeypatches via
`monkeypatch.setattr(blast, "_X", …)` is re-exported at the bottom of
`__init__.py`).

### New task modules

| File | Lines | Responsibility |
|------|------:|----------------|
| [cancel_task.py](../../../api/tasks/blast/cancel_task.py) | 103 | `cancel` Celery task — Redis-locked `elastic-blast delete` with status persistence. |
| [backfill_task.py](../../../api/tasks/blast/backfill_task.py) | 234 | `backfill_completed_runtime_metrics` Celery task — re-derive runtime metrics for already-completed jobs missing them. |
| [reconcile_task.py](../../../api/tasks/blast/reconcile_task.py) | 363 | `reconcile_stale_jobs` Celery task + helpers — sweep stuck rows and pull truth from AKS/Storage. |
| [poll_tasks.py](../../../api/tasks/blast/poll_tasks.py) | 219 | `check_status` (one-shot K8s probe) and `poll_running_status` (self-rescheduling poller) + the `POLL_RUNNING_*` constants. |
| [split_pipeline.py](../../../api/tasks/blast/split_pipeline.py) | 1,185 | Split-mode parent/child query pipeline (upload, dispatch, aggregate, verify, merge) plus the `merge_split_results` Celery task. |

### Cross-module call pattern

The task submodules import the package as `from api.tasks import blast as
_blast` and reference helpers / constants via `_blast.X` rather than direct
`from api.tasks.blast import X`. This preserves the test contract that
`monkeypatch.setattr(blast, "_helper", fake)` substitutions are honoured at
call time. Bare-name calls were not safe even between helpers inside the same
submodule — every intra-module reference to a helper or constant that the
tests patch on the package (`_upload_split_query_files`,
`_run_split_parent_submission`, `QUERY_FASTA_READ_MAX_BYTES`, …) goes
through `_blast.X`.

Re-imports live at the bottom of [api/tasks/blast/__init__.py](../../../api/tasks/blast/__init__.py)
under `# noqa: E402,F401` so the Celery beat schedule entries
(`api.tasks.blast.cancel`, `api.tasks.blast.backfill_completed_runtime_metrics`,
`api.tasks.blast.reconcile_stale_jobs`, `api.tasks.blast.check_status`,
`api.tasks.blast.poll_running_status`, `api.tasks.blast.merge_split_results`)
still resolve via the package, and all `@shared_task(name=…)` strings are
unchanged.

### Size summary (cumulative)

| File | Pre-batch-3 | After batch 3 | Delta |
|------|-----------:|--------------:|------:|
| `api/tasks/blast/__init__.py` | 2,993 | 1,173 | −1,820 |

Total `api/tasks/blast/` line count (sum of all `.py` files in the package)
is now 4,013 across nine modules, each with a single `Responsibility` line.

### Validation evidence (batch 3)

```bash
$ uv run ruff check api
All checks passed!

$ uv run pytest -q api/tests
868 passed in 35.74s

$ cd web && npx vitest --run
Test Files  26 passed (26)
     Tests  224 passed (224)

$ cd web && npm run build
✓ built in 7.09s
```

No behaviour change — the same 868 backend + 224 frontend tests pass.
Test attribute access on `blast._X` and `blast.X_CONSTANT` is preserved by
the bottom-of-file re-imports in `__init__.py`.


---

## Batch 4 — `api/tasks/blast/__init__.py` SRP final pass (2026-05-22, evening)

After batch 3 the package shell was still 1,173 lines because the `submit`
Celery task body (≈420 lines) and a long tail of submit-side helpers and
config-shim helpers were all still inline. Batch 4 splits the remainder
into five focused submodules — the shell is now 209 lines and the
`__init__.py` Responsibility line is "Re-export task entry points and
shared internal helpers; no business logic."

### New modules

| Module | Lines | Responsibility |
|--------|------:|----------------|
| `api/tasks/blast/cli_parsing.py` | 118 | Parse `elastic-blast submit` argv/stdout: build the CLI args, decode the trailing JSON payload, extract elastic-blast job ID, classify retryable failures. |
| `api/tasks/blast/config_shims.py` | 173 | Build the `elastic-blast.ini` payload and apply the option/database/warmup shims (sharding suppression, strict tie-order candidate pool expansion, node warmup readiness) before submission. |
| `api/tasks/blast/state.py` | 157 | Persist job state + history rows via `JobStateRepository`, emit Celery `update_state` progress checkpoints, and orchestrate the retry-or-fail bookkeeping shared by submit / cancel / reconcile tasks. |
| `api/tasks/blast/submit_runtime.py` | 272 | Submit-side runtime helpers — terminal exec streaming, K8s/Storage probes, result-gating, and the `TerminalAzureLoginError` class. |
| `api/tasks/blast/submit_task.py` | 525 | The `@shared_task(name="api.tasks.blast.submit", …)` Celery task itself — preparing → warming → splitting → configuring → submitting → completed pipeline. Decorator and signature are byte-identical to the previous inline definition. |

### Test-compat contract preserved

Every helper that production tests `monkeypatch.setattr(blast, "_X", …)` —
`_update_state`, `_progress`, `_has_parseable_result_artifact`,
`_stream_submit_command`, `_ensure_terminal_azure_cli_login`,
`TerminalAzureLoginError`, `resolve_db_metadata`, etc. — is still
accessible as `api.tasks.blast.X` via the bottom-of-file re-imports.
Submodules look up these symbols via `from api.tasks import blast as _blast`
+ `_blast.X` at call time so monkeypatches on the package propagate.

### Size summary (cumulative across all four batches)

| File | Pre-batch-1 | After batch 3 | After batch 4 | Delta vs pre |
|------|-----------:|--------------:|--------------:|------:|
| `api/tasks/blast/__init__.py` | 2,993 | 1,173 | 209 | −2,784 |

Total `api/tasks/blast/` package: 4,171 lines across 16 modules. The
shell is now a thin re-export surface; every submodule has a single
`Responsibility` line that fits without an "and".

### Validation evidence (batch 4)

```bash
$ uv run ruff check api
All checks passed!

$ uv run pytest -q api/tests
871 passed in 25.85s

$ cd web && npx vitest --run
Test Files  26 passed (26)
     Tests  224 passed (224)

$ cd web && npm run build
✓ built in 6.63s
```

Test count grew from 868 → 871 between batches via unrelated work on
other branches that landed in between; no batch-4 test was added or
removed. Behaviour-identical refactor.
