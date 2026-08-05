---
title: Page the cluster-wide Kubernetes LISTs that OOM-killed the api sidecar
description: The pods/deployments/jobs monitor calls fetched every object in the cluster with no limit and materialised the whole JSON, making api memory O(cluster size). Page them, cap the item count, gate concurrency, and add a source guard so it cannot come back.
tags: [operate, infra, ui]
---

# Page the cluster-wide Kubernetes LISTs that OOM-killed the api sidecar

## Motivation

The [arena reclaimer](2026-08-05-api-sidecar-arena-reclaim.md) stopped the api
sidecar's slow RSS ratchet but explicitly did **not** reduce peak allocation,
and the sidecar kept being SIGKILL'd (exit 137) under load. This change fixes
the peak.

### How the cause was actually found (the first two hypotheses were wrong)

1. **Wrong hypothesis A — Storage usage enumeration.** `tracemalloc` was run with
   the default single frame, so it only reported library *leaf* lines
   (`urllib3/response.py:185`, `json/decoder.py:354`). Those were attributed to
   `container_usage_summaries` walking every blob. But
   [usage_cache.py](../../../api/services/storage/usage_cache.py) is a TTL-300 s
   stale-while-revalidate cache refreshed on a background thread — it cannot
   drive a 4-second poll cadence.
2. **Wrong hypothesis B — result parsing / `database_list`.** App Insights
   `requests` for the OOM window (14:38-14:44 UTC) contained **no**
   `/api/blast/databases`, no `/api/monitor/storage` and no result-analytics
   call. `database_list`'s `list_blobs()` is an `ItemPaged` generator, so it
   already streams.
3. **The evidence that settled it.** Three independent signals agreed:
   * Requests in flight around each kill were all **Kubernetes-backed** monitor
     routes (`aks/warmup-status`, `aks/top-nodes`, `message-flow`, `blast/jobs`).
   * OOM kills track cluster state, not dashboard traffic:

     | Date | api OOM kills | AKS |
     | --- | --- | --- |
     | 07-31 | **56** | running, heavy BLAST |
     | 08-03 | 1 | mostly stopped |
     | **08-04** | **0** | **stopped** |
     | 08-05 | 17 | started 12:33 for a test |
   * The code: [k8s/monitoring.py](../../../api/services/k8s/monitoring.py)
     issued `GET {server}/api/v1/pods`, `/apis/apps/v1/deployments` and
     `/apis/batch/v1/jobs` — **cluster-wide, no `limit`, no `continue` paging** —
     then `response.json()`. The pod list deliberately includes Succeeded
     (Completed) pods for Azure-portal parity, so it grows with every BLAST run.

Peak memory was therefore **O(cluster object count)** inside a 2 GiB process,
polled by every open dashboard tab.

## User-facing change

Cluster workload tabs (Pods / Deployments / Jobs) and the diagnostics snapshot
now render from a paged fetch. Behaviour is unchanged for any cluster below the
item ceiling; beyond it the list is capped and a WARNING names the label and the
counts, so truncation is visible rather than silent.

## Change summary

* New [api/services/k8s/paged_list.py](../../../api/services/k8s/paged_list.py) —
  `list_k8s_items()`:
  * `limit` + `continue` paging, so **one HTTP response stays bounded regardless
    of cluster size**. This is the actual fix; everything else is defence.
  * Total item ceiling (`K8S_LIST_MAX_ITEMS`, default 5000) with a truncation
    WARNING — no dashboard table renders more, and silent loss is worse than a
    capped list.
  * Bounded page loop (`_MAX_PAGES = 50`) so a server that always hands back a
    continue token — or a bug in our own paging — cannot spin forever.
  * HTTP 410 mid-pagination (expired continue token) degrades to the pages
    already collected instead of blanking the card.
  * Concurrency gate (`K8S_LIST_MAX_CONCURRENCY`, default 6) with a **bounded**
    acquire that raises `K8sListBusy` rather than stalling an api worker. 6, not
    4, because `diagnostics.snapshot` fans out three gated calls at once.
  * Oversized-page WARNING, so the next investigation reads the size off a log
    line instead of re-deriving it from `tracemalloc`.
* [api/services/k8s/monitoring.py](../../../api/services/k8s/monitoring.py):
  `k8s_get_pods` / `k8s_get_deployments` / `k8s_get_jobs` fetch through the
  helper. Return types are unchanged (`list[dict]`), so no consumer changes.

### Consumers checked

`routes/monitor/aks.py` (`_graceful` + `cached_snapshot_with_cluster_gate`),
`services/diagnostics/snapshot.py` (per-list `try/except` with a `*_error`
marker) and `services/blast/capacity_signals.py` (`except Exception` → 0) all
already degrade on exceptions, so `K8sListBusy` surfaces as an empty card, never
a 500.

## Deliberately NOT changed

* **`usage.py` / `database_list.py`** — hypotheses A and B above. `usage.py` is
  cached and capped; `database_list.py` streams via `ItemPaged` and genuinely
  needs `blob.size` / `last_modified`, so `list_blob_names()` is not applicable.
  Changing them would be churn with no measured benefit.
* **`metrics.k8s.io` pod metrics** — the aggregated API does not implement
  `limit`/`continue`, and a PodMetrics object is a name plus per-container usage
  (~200 B). Paging it would add a loop that the server ignores.
* **Monitor route caching** — already implemented via
  `cached_snapshot_with_cluster_gate`.
* **Raising the memory limit again** — done twice already (1 Gi → 2 Gi); it only
  moves the wall.

## Validation

* `uv run pytest -q api/tests/test_k8s_paged_list.py` — 8 passed: every request
  carries a `limit`; continue tokens are followed and forwarded; the item cap
  truncates *and* logs; the page loop terminates against a server that never
  stops paging; a 410 yields a partial result; an oversized page warns; the gate
  sheds with `K8sListBusy` instead of stalling; and the gate permit is released
  even when the transport raises.
* `uv run pytest -q api/tests/test_k8s_list_bounds_guard.py` — 3 passed. A source
  guard fails the build on any new unpaged cluster-wide LIST, plus a
  self-check that the regex still matches the three known endpoints and does not
  flag namespaced URLs. Genuinely-bounded endpoints live in `_ALLOWED_UNPAGED`
  **with their reason**.
* `api/tests/test_k8s_get_pods.py` — the phase-parity test asserted "no `params`
  at all" as a proxy for "no `fieldSelector`". Corrected to assert
  `fieldSelector` specifically (the real contract) and a new
  `test_k8s_get_pods_is_paged` pins the `limit`.
* `uv run pytest -q api/tests` — **4979 passed**, 3 skipped.
* `uv run ruff check api` — clean.

## Follow-up

Not deployed with this change. The remaining known peak-allocation path is the
BLAST result analysis in `routes/blast/result_analytics.py`, which passes a fully
materialised `str` into `parse_blast_result_content`. The parser itself already
uses `iterparse`, so only the input needs to become a stream —
[split_pipeline.py](../../../api/tasks/blast/split_pipeline.py) already does
exactly that on the worker side. That path is not polled, so it did not
contribute to this incident.
