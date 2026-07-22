---
title: In-memory mTLS client cert for pooled K8s sessions
description: Load the AKS admin/local-account client certificate and key into an
  in-memory SSLContext so pool eviction can no longer delete a cert file a
  borrowed session still references — closing the /tmp/elb-k8s-*.crt race that
  gated Service Bus warm-up drains.
tags:
  - operate
  - architecture
---

# In-memory mTLS client cert for pooled K8s sessions

## Motivation

Sharded `core_nt` + taxonomy-filter (`-taxids` / `-negative_taxids`) BLAST
requests submitted through the Service Bus integration intermittently failed to
receive a completion event during a cluster warm-up window. Investigation
(App Insights + Container App console logs, incident window 2026-07-22
04:00–05:00 UTC) traced the gating to a temp-file race in the pooled Kubernetes
API session helper:

* `k8s_warmup_status` failed repeatedly with
  `Could not find the TLS certificate file, invalid path: /tmp/elb-k8s-*.crt`
  (17 occurrences in 24 h, ongoing on the deployed image). Each failure used a
  different temp filename — the signature of `NamedTemporaryFile`.
* A failed warm-readiness read made the Service Bus drain admission defer with
  `reason=cluster_warming`, so submissions/completion events were delayed for
  the duration of the warm-up window (04:06–04:38 UTC saw zero successes; two
  requests were briefly abandoned at 04:38 and recovered at 04:46).

Issue #47 previously moved the cluster **CA** into an in-memory `SSLContext`
because pooled sessions outlive a single request and pool eviction / `atexit`
unlink the backing temp files at TTL expiry — a request still holding a borrowed
session could read a `session.verify` path that had already been deleted. The
**mTLS client certificate / key** of admin and local-account kubeconfigs were
left on exactly the same racy footing: `_get_k8s_session` wrote them to
`/tmp/elb-k8s-*.crt|.key` and set `session.cert = (cert_path, key_path)`, so
eviction could delete the cert out from under an in-flight warm-readiness poll.

`elb-cluster-01` returns a client-cert-based kubeconfig from
`list_cluster_user_credentials`, so the default (`admin=False`) warm-up polls
took the client-cert path and hit the race.

## User-facing change

None directly. Warm-up readiness reads on client-cert clusters no longer fail on
a deleted temp cert, so the Service Bus drain stops deferring spuriously during
warm-up and sharded taxid-filtered BLAST completions arrive without the
warm-window delay.

## API / IaC diff summary

* `api/services/k8s/client.py`
  * New `_load_client_cert_into_context()` writes the client cert / key to
    `mkstemp` files **only** long enough for `ssl.SSLContext.load_cert_chain`
    to parse them into memory, then unlinks both immediately in a `finally`
    block.
  * `_build_k8s_https_adapter()` now accepts optional `client_cert` /
    `client_key` and folds them into the same in-memory `SSLContext` that
    already carries the CA. It also tolerates a `None` CA (falls back to system
    roots) so the client-cert-only path is covered.
  * `_get_k8s_session()` routes client cert / key through the adapter and no
    longer sets `session.cert` or writes credential temp files. A pooled entry
    now owns **zero** temp files, so eviction / `atexit` has nothing to unlink.
* `api/tests/test_k8s_session_pool.py`
  * Added a real self-signed client cert/key PEM fixture (the placeholder bytes
    no longer parse now that `load_cert_chain` runs).
  * Reworked the two throwaway-path tests to assert no cert lands on disk and
    `session.cert` is never set.
  * New regression test
    `test_client_cert_in_memory_survives_pool_eviction` drives a GET on a
    borrowed admin session **after** the pool is drained and asserts the
    request carries no filesystem cert path (the old `OSError`).

No IaC change.

## Scope note — drain blocking (#2) and warm-readiness robustness (#3)

The 55-minute `SoftTimeLimitExceeded` drain block and the herd/revoke churn seen
in the incident logs were downstream symptoms of the deployed image plus the
client-cert race above, **not** a missing mitigation in the current tree:

* `_drain_once` already returns immediately (`{"skipped": <reason>}`) when
  admission defers — it never blocks on a warm-up wait loop.
* A single-flight lease (`_acquire_drain_lock`) already skips overlapping ticks,
  and the beat tick carries a 30 s `expires` so stale ticks are shed rather than
  replayed.
* `k8s_warmup_status` already degrades to `warm=False` on any read exception
  instead of crashing.

Deploying the current tree (with this fix) removes the cause of the warm-window
deferral; no additional drain/readiness code change was required.

## Validation

* `uv run pytest -q api/tests/test_k8s_session_pool.py api/tests/test_k8s_list_events.py`
  — 18 passed.
* `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py api/tests/test_servicebus_tasks.py`
  — green (one wall-clock timing assertion in
  `test_warmup_status_parallel_pod_logs` flaked at 0.441 s vs its 0.4 s
  "generous" bound and passed on rerun; unrelated to this change, which is
  mocked out in that test).
* `uv run ruff check api/services/k8s/client.py api/tests/test_k8s_session_pool.py`
  — all checks passed.
