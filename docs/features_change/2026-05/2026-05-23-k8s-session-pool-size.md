# K8s pooled session — bump HTTPAdapter pool_maxsize to 32

## Motivation
`_get_k8s_session` returned a `requests.Session()` with the default
`HTTPAdapter(pool_maxsize=10)`. `k8s_warmup_status` immediately fans out
6 concurrent GETs on a single shared session, and `_warmup_pods_and_logs`
adds more concurrent per-pod log requests on the same session. With two
dashboard pollers in flight the urllib3 connection pool saturated within
seconds, forcing a fresh TLS handshake for every over-cap GET — the
hottest monitor route was unwittingly paying handshake latency on every
poll.

## User-facing change
None. Lower steady-state CPU + latency on every `/api/monitor/aks/*`
poll under concurrent dashboard load.

## API / IaC diff
* `api/services/k8s/client.py`
  * New constant `_K8S_SESSION_HTTP_POOL_SIZE = 32` + env override
    `K8S_SESSION_HTTP_POOL_SIZE` (1..256).
  * `_get_k8s_session` mounts a `requests.adapters.HTTPAdapter(
      pool_connections=_pool_size, pool_maxsize=_pool_size,
      pool_block=False
    )` on both `http://` and `https://` for every pooled session.
    `pool_block=False` preserves the urllib3 default of "allocate
    over-the-cap connections rather than wait", which keeps a brief
    burst from stalling at the cost of a few short-lived sockets.

## Validation
* `uv run pytest -q api/tests/test_k8s_list_events.py
  api/tests/test_k8s_warmup_status_parallel.py
  api/tests/test_k8s_release_stale_warmup_jobs.py` — 14 passed.
* `uv run ruff check api/services/k8s/client.py` — clean.
