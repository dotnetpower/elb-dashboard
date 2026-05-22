# frontend_proxy — stream the upstream response instead of buffering

## Motivation
The reverse proxy used `await client.request(...)` which loads the
entire upstream response body into memory before returning. SPA dev
bundles (source maps, wasm) can be several MB; N concurrent first loads
multiplied through the api sidecar's RAM. The behavior was also
load-bearing for the `304` short-circuit since `request()` always
buffers — there was no genuine streaming path even in production.

## User-facing change
None functionally. Lower api sidecar RAM under concurrent first loads
and the time-to-first-byte is now bounded by the frontend nginx response
start instead of the full asset length.

## API / IaC diff
* `api/routes/frontend_proxy.py`
  * Switched from `await client.request(...)` to
    `client.build_request(...)` + `await client.send(req, stream=True)`.
  * Returns a `StreamingResponse(_body_iter())` whose generator pipes
    `upstream_resp.aiter_raw()` chunks straight through and runs
    `upstream_resp.aclose()` in a `finally` so the upstream connection
    always returns to the pool even on early client disconnects.
  * 304 path still emits an empty body but now explicitly
    `await upstream_resp.aclose()` before returning.
* `api/tests/test_security_audit_bundle.py`
  * `_RecordingAsyncClient` stub updated to mirror the new contract
    (`build_request` + `send` returning a `ByteStream`-backed response).

## Validation
* `uv run pytest -q api/tests/test_security_audit_bundle.py` — 9 passed.
* `uv run ruff check api/routes/frontend_proxy.py
  api/tests/test_security_audit_bundle.py` — clean.
