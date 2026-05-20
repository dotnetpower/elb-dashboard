# BLAST Submit Lock Scope

## Motivation

A second BLAST submit could surface `blast_submit_lock_busy` after the previous `elastic-blast submit` process had already exited. The Redis submit lock was still held while the worker synchronously persisted large submit log chunks to Azure Table/Blob storage.

## User-facing change

Back-to-back BLAST submissions no longer wait on post-submit log artifact persistence. The submit lock still serializes the actual `elastic-blast submit` command, but log chunk artifacts are batched and written after the lock is released.

## API/IaC diff summary

- Updated the BLAST Celery submit task to collect streamed submit log events in memory during the terminal command.
- Deferred execution log artifact writes until after releasing the Redis submit lock.
- Batched deferred submit log artifact writes in fixed-size chunks.
- Reduced live job-state writes during submit streaming to a bounded interval.
- No API shape, route, or IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_tasks.py -k "stream_submit_command_defers_log_artifact_writes or persist_submit_log_events_chunks_after_stream or update_state_uses_repository_contract"` — 3 passed.
- `uv run pytest -q api/tests/test_blast_tasks.py` — 86 passed.
- `uv run ruff check api/tasks/blast/__init__.py api/tests/test_blast_tasks.py` — passed.
- `uv run ruff check api` currently fails on pre-existing E501 line-length violations in `api/tests/test_storage_public_access.py`.
