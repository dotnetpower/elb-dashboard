# Live Wall logs: fix LA-fallback self-deadlock on first fetch

## Motivation

In the deployed Container App, the Live Wall page rendered CPU/MEM metrics for
all six sidecar tiles but showed **no log lines** on any tile ("no recent
activity", source shown as "live"). No error was logged in the `api` sidecar.

All infrastructure was confirmed healthy:

- `LOG_ANALYTICS_WORKSPACE_ID` set on the `api` sidecar.
- Shared MI holds `Log Analytics Reader` at the workspace scope.
- The exact KQL the code runs returns hundreds of rows for the active sidecars.
- The deployed image was built from the commit that contains the LA-fallback
  code, with `azure-monitor-query` present.
- A local probe of `read_recent_lines_la` (with a working credential
  **pre-injected** into `sidecar_logs_la._client`) returned real lines
  (api → 60, worker → 60, beat → 37, terminal → 60).

## Root cause

`api/services/sidecar_logs_la.py` used a single non-reentrant
`threading.Lock` (`_lock`) for both the snapshot refresh and the lazy
`LogsQueryClient` construction:

- `_ensure_snapshot()` acquires `_lock`, then **inside that locked block**
  calls `_fetch_snapshot()` → `_get_client()`.
- On the **first** fetch (`_client is None`), `_get_client()` tried to acquire
  the **same `_lock`** again → permanent self-deadlock.

The SSE log stream's worker thread (`asyncio.to_thread(read_recent_lines, …)`)
blocked forever on that first fetch: the EventSource stayed open at HTTP 200
("live"), never emitted a `line` event, and raised no exception — so nothing
was logged. The unit tests never caught it because they monkeypatch
`_get_client` entirely, bypassing the real lock path.

The local probe worked only because it pre-set `sidecar_logs_la._client`, so
`_get_client()` returned early without touching the lock.

## User-facing change

Live Wall log tiles now stream real per-sidecar log lines in the deployed
Container App instead of staying perpetually empty.

## Code diff summary

- `api/services/sidecar_logs_la.py`: added a dedicated `_client_lock` for lazy
  client construction so `_get_client()` no longer re-acquires the
  snapshot-refresh `_lock`. Documented why the two locks must stay distinct.
- `api/tests/test_sidecar_logs_la.py`: added
  `test_first_fetch_does_not_deadlock_on_lazy_client`, which runs the **real**
  `_get_client()` path (only the credential + SDK constructor stubbed) from a
  worker thread with a join timeout, so a regression of the deadlock fails the
  test instead of hanging CI.

No IaC change.

## Validation

- `uv run pytest -q api/tests/test_sidecar_logs_la.py api/tests/test_sidecar_logs.py`
  → 18 passed (includes the new regression test).
- `uv run ruff check api/services/sidecar_logs_la.py api/tests/test_sidecar_logs_la.py`
  → all checks passed.
