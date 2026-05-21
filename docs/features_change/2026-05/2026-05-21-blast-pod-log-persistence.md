# 2026-05-21 — Persist k8s pod logs at BLAST job finalization

## Motivation

After [2026-05-21 BLAST log discovery fix](./2026-05-21-blast-log-discovery.md)
the live SSE stream now finds `blast` / `results-export` / `init-ssd` pods
correctly while a job is running, but those pod logs were still **live-only**.
Once the SSE client disconnected or the job reached a terminal phase, the
real BLAST execution output evaporated — only `staging_db.last_output`
(the submit task's azcopy stdout) survived. That is the literal second
half of the user complaint *"실행 로그가 azcopy 로그 정도만 나오는데"*.

## User-facing change

When a BLAST job transitions to a terminal phase (`completed` / `failed`),
the artifact finalizer now fetches a tail of every matching pod/container
log via the Kubernetes API and persists it to:

1. **Per-step `last_output` summary** in `payload._progress.steps.<phase>`
   (truncated to ~6 KB with head + tail markers if long). This is what
   the *Running* / *Exporting Results* step log block shows in the UI.
2. **Chunked log artifacts** in the platform Storage account at
   `{job_id}/execution-steps/logs/<phase>/<seq>.json` (≤ 100 events per
   chunk). These mirror the format already used by the submit task and
   keep the full tail (up to 200 lines per container) for forensic
   review even when the inline summary is truncated.

Containers included in the inline summary are limited to the "primary"
producers per phase so the panel stays focused:

| Phase | Primary containers |
| --- | --- |
| `running` | `blast`, `results-export` |
| `exporting_results` | `results-export` |
| `staging_db` | `get-blastdb`, `import-query-batches` |
| `warming_up` | `vmtouch` |

Other containers (`logger`, sidecar shims) still land in the chunked
artifacts but stay out of the summary blob.

## API / IaC diff summary

- New module
  [api/services/job_logs/persist.py](../../../api/services/job_logs/persist.py)
  with `persist_completed_job_pod_logs(credential, state)`. Idempotent;
  never raises (all I/O failures degrade to logged `INFO`).
- [api/services/job_logs/k8s.py](../../../api/services/job_logs/k8s.py):
  added `fetch_k8s_pod_log_tail(...)` — a `follow=false` variant of
  `stream_k8s_log_lines` that safely fetches the tail of terminated pods.
- [api/tasks/blast_artifacts.py](../../../api/tasks/blast_artifacts.py):
  `finalize_job_artifacts` calls the persist helper *before*
  `write_execution_steps_snapshot` so the snapshot picks up the merged
  `last_output` blobs. Errors are caught and logged.
- No Bicep / IaC changes. No new dependency. No browser code change.

## Security

- All Kubernetes calls go through the existing `_get_k8s_session` helper
  (shared user-assigned MI, no Run Command, charter §11).
- Lines are sanitised via `api.services.sanitise.sanitise(...)` and
  truncated to 4 000 chars per line before persistence — same contract
  as the live SSE path.
- Storage writes go through the existing `write_execution_log_chunk` /
  `repo.update` codepaths which already use the platform MI and private
  endpoints. `publicNetworkAccess` remains `Disabled`.

## Validation

- `uv run pytest -q api/tests/test_job_log_persist.py` → 4 new tests pass
  (groups by phase, writes chunks, skips when inputs missing, skips when
  no targets, does not clobber longer existing `last_output`).
- `uv run pytest -q api/tests` → 864 passed (full suite).
- `uv run ruff check api` → clean.

## Out of scope

- Re-fetching logs after pod garbage-collection. Kubernetes deletes
  completed pods after ~6 h by default. If the finalizer runs after the
  pod is gone, the helper logs an `INFO` and falls back to whatever was
  already persisted by the live SSE path. A "log retention via
  CronJob → blob copy" pattern would close that window but is a
  separate IaC change.
- Streaming live `last_output` updates while the job is still running.
  Persistence is intentionally a single tail snapshot at finalize; live
  monitoring continues to flow through SSE as before.
