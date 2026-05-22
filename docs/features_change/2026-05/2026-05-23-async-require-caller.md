# require_caller — async + lazy threadpool offload

## Motivation
`require_caller` was a sync FastAPI dependency. Every authenticated
request consumed one of starlette's threadpool slots (default 40) for
the full duration of the JWT validation — and the cold-path JWKS fetch
inside `_get_jwks_client` did synchronous network IO via `httpx.Client`.
Under sustained dashboard polling (multiple users × multiple monitor
routes × 14 s poll cadence) the threadpool became the bottleneck for
HTTP throughput.

## User-facing change
None functionally. Lower latency on cache-hit auth (no threadpool round
trip), and the event loop stays responsive for SSE / WebSocket /
streaming responses even when many JWT validations are in flight at
once.

## API / IaC diff
* `api/auth.py`
  * `require_caller` is now `async def`.
  * Cache-hit path returns directly without touching `asyncio.to_thread`
    (the only IO is a dict lookup behind a lock).
  * Cache-miss path runs `_validate_token` via `asyncio.to_thread` so
    the synchronous JWKS fetch + JWT decode does not block the event
    loop.
* `api/tests/test_auth_caching.py`
  * The two tests that called `require_caller(...)` directly now wrap
    the call in `asyncio.run(...)` to drive the coroutine.

## Validation
* `uv run pytest -q api/tests/test_auth_caching.py api/tests/test_smoke.py`
  — 85 passed.
* `uv run ruff check api/auth.py api/tests/test_auth_caching.py` — clean.
* Pre-existing `api/tasks/upgrade/reconciler.py` references a missing
  `api.tasks._upgrade_pipeline` module — unrelated to this change, see
  in-flight upgrade refactor.
