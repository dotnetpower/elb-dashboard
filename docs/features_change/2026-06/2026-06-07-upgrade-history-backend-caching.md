---
title: Upgrade history/build-log backend caching — stop redundant create_container
description: Cache the lazily-created Azure append-blob backend in the upgrade history and build-log services so a fresh BlobServiceClient.create_container is not issued on every record_event / append / tail call.
tags:
  - operate
  - architecture
---

# Upgrade history/build-log backend caching — stop redundant create_container

## Motivation

An App Insights dependency-failure hunt on the live deployment surfaced
`BlobServiceClient.create_container` being called **~1224 times in 4 hours** from
the `elb-api` role — by far the noisiest Storage dependency, even though the
container only needs to be ensured once per process.

## Root cause

Both [api/services/upgrade/history.py](../../../api/services/upgrade/history.py)
and [api/services/upgrade/build_logs.py](../../../api/services/upgrade/build_logs.py)
expose a module-level `_backend()` that returns the configured backend when one
was injected via `set_backend()` (tests do this), and otherwise lazily builds
the Azure append-blob backend:

```python
def _backend() -> _Backend:
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            return _BACKEND
        return _AzureAppendHistoryBackend()   # ← built fresh EVERY call, never cached
```

The `_Azure*Backend` instances carry a per-instance `_ensured` flag that guards
the one-time `create_container`. Because the lazy instance was **never stored in
`_BACKEND`**, every `record_event` / `tail_events` / build-log `append` built a
brand-new backend with `_ensured = False`, so each call re-issued
`create_container`. The upgrade beat reconciler (180 s), status polling, and
history-tail reads multiply this into ~1200 redundant calls per 4 h.

## Fix

Cache the lazily-created instance in `_BACKEND` (declaring `global _BACKEND` at
the top of `_backend()`), so the container is ensured exactly once per process.
Tests still reset via `set_backend(None)`.

## Validation

- New regression test `test_backend_is_cached_across_calls` asserts the backend
  is built exactly once across repeated `_backend()` calls.
- `uv run pytest -q api/tests/test_upgrade_history.py api/tests/test_upgrade_build_logs.py api/tests/test_upgrade_chaos.py` — 27 passed.
- `uv run ruff check` clean on both files.
- Expected effect: the `BlobServiceClient.create_container` dependency volume
  drops from ~1200/4h to ~1 per process start.

## Notes

This is a telemetry-noise + redundant-Storage-call fix; it changes no
user-visible behaviour and no audit-chain semantics (the append/read paths are
unchanged — only the backend instance is now reused).
