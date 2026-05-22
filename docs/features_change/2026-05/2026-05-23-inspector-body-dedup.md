# request-detail inspector — lazy slice (drop the duplicate body buffer)

## Motivation
The request inspector middleware previously did:

```python
raw = await request.body()
captured_request_body = raw[:_INSPECTOR_MAX_BUFFER_BYTES]  # copy #2
async def _replay_receive(_body: bytes = raw): ...          # holds copy #1
request._receive = _replay_receive
```

For a multi-MB upload that meant *two* copies of the body resided in
RAM for the whole route lifetime — once as the captured slice that the
emitter eventually persisted, once as the closure body the route handler
re-read. Streaming the body away from the middleware entirely is
incompatible with the `test_middleware_captures_post_body_and_route_returns_it`
contract (404 routes never read the body, so a chunk-level inspector
would not capture them).

## User-facing change
None. Inspector still captures up to `_INSPECTOR_MAX_BUFFER_BYTES`; the
route handler still sees the full original body. RAM cost on the
inspector side drops from `2 × body_size` to `body_size`.

## API / IaC diff
* `api/main.py` `RequestIdMiddleware`
  * Replace `captured_request_body` (eager prefix slice) with `raw_body`
    (single bytes object).
  * New `_captured_body_bytes()` closure produces the prefix slice on
    demand — only when an emitter actually consumes it, and only as a
    new allocation when the body exceeds the cap.

## Validation
* `uv run pytest -q api/tests/test_request_metrics_detail.py
  api/tests/test_smoke.py` — 86 passed.
* `uv run ruff check api/main.py` — clean.
