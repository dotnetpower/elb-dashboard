---
title: Clearable notifications with failure detail
description: The notification center can hide its current feed without deleting job history and now shows sanitised human-readable failure context alongside machine codes.
tags:
  - ui
  - blast
---

# Clearable notifications with failure detail

## Motivation

"Mark all read" only cleared the unread badge, so completed and failed items
continued to occupy the dropdown with no way to remove them. Failed items also
showed only a machine classification such as `cluster_lifecycle_interrupted`,
even when the job state contained a human-readable reason and remediation.

## User-facing change

- "Clear all" hides every notification currently in the dropdown. It does not
  delete BLAST or warmup job history; jobs remain available from their normal
  history and result surfaces.
- Failed notifications show the machine error code plus the sanitised detail
  recorded by the failing task.
- If a read/clear preference cannot be saved, the action now reports a retryable
  error instead of briefly changing the UI and then letting the old state return.

## API and storage diff

- `POST /api/notifications/clear` stores a per-user `cleared_before_at` cutoff in
  the existing `notifseen` table and returns the new cutoff.
- Seen and clear actions update independent marker fields with Azure Table
  `MERGE`, so concurrent requests cannot erase or reorder each other's state.
- Feed assembly keeps the payload-free summary scan, then batch-fetches only the
  visible failed rows to extract `payload.error`. The detail is truncated and
  passed through the shared output sanitiser before it enters the response.
- `error_detail` is additive and optional for backward-compatible clients.

## Validation evidence

- `uv run pytest -q api/tests/test_notifications.py`
- `uv run ruff check api`
- `cd web && npm test -- --run`
- `cd web && npm run build`
