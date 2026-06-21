# Fix 500 + connection leak when downloading a result file from a stopped cluster

**Date:** 2026-06-21

## Motivation

While validating the Service Bus completion → consumer download flow, following a
completion event's `download_url` **after the AKS cluster auto-stopped** returned
HTTP **500 `internal server error`** instead of a clean, retryable error. The api
log showed:

```
UnboundLocalError: cannot access local variable 'client' where it is not associated with a value
  File "api/services/external_blast.py", line 1147, in stream_file
```

### Root cause

`stream_file` proxies the download to the elb-openapi pod. Its inner `_open()`
helper created an `httpx.Client` and then called `client.send()`. When the
cluster is stopped the openapi pod is gone, so `client.send()` raised
`httpx.ConnectError` **inside** `_open()` — before the outer
`client, resp = _open(...)` assignment completed. The `except httpx.HTTPError`
block then referenced the still-unbound `client` to call `client.close()`,
raising `UnboundLocalError` (a 500) instead of the intended 503
`openapi_unreachable`. The `httpx.Client` created inside `_open()` was also
**leaked** (never closed) on every unreachable-openapi download — a connection /
file-descriptor leak in the api sidecar that recurs on every auto-stop.

## User-facing change

- Downloading an external/Service Bus result file while the cluster is stopped
  now returns a clean **503 `openapi_unreachable`** (retryable, honest) instead
  of a 500, and no longer leaks a connection pool.
- The result download still requires the cluster to be running (the proxy goes
  through the openapi). A consumer that downloads promptly after completion is
  unaffected; one that waits until after auto-stop gets a clear 503. The deeper
  "serve results from Storage after auto-stop" gap is tracked separately.

## Code change summary

- `api/services/external_blast.py` — `stream_file`:
  - `_open()` closes its just-created client if `client.send()` raises (no leak).
  - The outer `client` is initialised to `None` and the `except` blocks guard
    `client.close()` with `if client is not None`, so a connect failure surfaces
    as `HTTPException(503, openapi_unreachable)` instead of `UnboundLocalError`.
  - The 401-resync branch sets `client = None` after closing so a failed reopen
    cannot leave a stale reference.

No IaC or contract change.

## Validation

- `uv run pytest -q api/tests/test_external_blast_api.py` — 107 passed (incl. new
  `test_stream_file_unreachable_openapi_returns_503` asserting 503 + that the
  inner client is closed).
- Live repro: with `elb-cluster-01` stopped, `GET /api/v1/elastic-blast/jobs/
  9ca72c6092b0/files/result-001` returned 500 before the fix; returns 503
  `openapi_unreachable` after deploy.
