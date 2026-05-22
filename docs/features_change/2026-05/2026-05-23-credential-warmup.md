# lifespan — warm DefaultAzureCredential at startup

## Motivation
`DefaultAzureCredential(...)` is lazy. The first `get_token(...)` does
the synchronous IMDS / `az login` fallback chain — typically 1-3 s, up
to 10 s under contention. Without warm-up the first authenticated
request after process start paid that latency on the request thread
(through ``require_caller`` → Storage / ARM call).

## User-facing change
None for the cold-start request beyond avoiding the latency hit. Any
failure of the warm-up is silent — the next real request retries
through the normal path.

## API / IaC diff
* `api/main.py::_lifespan`
  * Spawn `app.state._cred_warmup_task = asyncio.create_task(_prime())`
    that runs `get_credential()` + `get_token("https://management.azure.com/.default")`
    via `asyncio.to_thread`, so uvicorn keeps accepting connections
    while the token is being fetched.
  * Reference retained on `app.state` per ruff RUF006 so the task is
    not GC'd while pending.

## Validation
* `uv run pytest -q api/tests/test_smoke.py api/tests/test_version.py`
  — 80 passed.
* `uv run ruff check api/main.py` — clean.
