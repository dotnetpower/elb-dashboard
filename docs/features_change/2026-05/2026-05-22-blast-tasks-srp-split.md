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

