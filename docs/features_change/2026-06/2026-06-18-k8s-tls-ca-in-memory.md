---
title: K8s cluster CA loaded in-memory to end TLS CA temp-file race
description: The pooled Kubernetes session now trusts the AKS cluster CA via an in-memory SSLContext instead of a temp file, eliminating the use-after-free OSError on the CA bundle.
tags:
  - operate
  - security
---

# K8s TLS CA in-memory (#47)

## Motivation

Intermittent `OSError: Could not find a suitable TLS CA certificate bundle,
invalid path: /tmp/elb-k8s-<rand>.crt` was raised from pooled Kubernetes session
usage. The pooled `requests.Session` stored the AKS cluster CA as a
`NamedTemporaryFile` path in `session.verify`. Pooled sessions outlive a single
request, and pool eviction / atexit cleanup unlinks those temp files at TTL
expiry — so a request still holding a borrowed session could read a
`session.verify` path that had already been deleted (a use-after-free style race
on the temp file).

## User-facing change

None directly. The affected k8s call no longer fails intermittently, and the
dashboard's AKS-backed cards stop emitting the medium-severity `OSError`.

## API / IaC diff summary

- `api/services/k8s/client.py`:
  - New `_build_k8s_https_adapter(ca_data, pool_size)` builds an HTTPS adapter
    whose pool manager uses an in-memory `ssl.SSLContext`
    (`load_verify_locations(cadata=...)`) — the cluster CA never lands on disk.
  - `_get_k8s_session` now mounts that adapter for the CA-trust path and keeps
    `session.verify = True`. The CA temp file (`write_secret_file(".crt", ca)`)
    is gone, so eviction has no CA bundle to unlink. Client cert / key files
    (admin mTLS path) are unchanged.

No infra change.

## Validation evidence

- `uv run pytest -q api/tests/test_k8s_session_pool.py` — 11 passed, including
  the new `test_ca_in_memory_survives_pool_eviction_during_inflight_get`, which
  asserts (a) the Bearer path writes zero temp files, (b) `session.verify is
  True`, (c) the https adapter carries the in-memory `SSLContext`, and (d) a GET
  on a borrowed session *after* the pool is drained still reaches the adapter
  with `verify=True` (never a deleted path).
- `uv run pytest -q api/tests/ -k "k8s or warmup or monitor or aks"` — 789 passed.
- `uv run ruff check` clean on the touched files.
