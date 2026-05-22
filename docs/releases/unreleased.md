---
title: Unreleased
description: ElasticBLAST Control Plane Unreleased release notes — feature-change notes that landed in this version.
tags:
  - release
---

# Unreleased

Feature-change notes added in `v0.2.0..HEAD`.

**Count:** 47

## Features

- `2026-05-22` — [BLAST DB download hardening — version preview, honest copy status, atomic promotion](../features_change/2026-05/2026-05-22-blast-db-download-hardening.md) ([`a408186`](https://github.com/dotnetpower/elb-dashboard/commit/a40818659dcd39aa38abae742b82867bd910c418))
- `2026-05-22` — [PR1 — self-upgrade read-only surface (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-pr1-read-only.md) ([`7afddae`](https://github.com/dotnetpower/elb-dashboard/commit/7afddaede463953aa9980b66241a2ae03d5de3db))
- `2026-05-22` — [PR2 — self-upgrade build pipeline (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-pr2-build-pipeline.md) ([`f706047`](https://github.com/dotnetpower/elb-dashboard/commit/f706047167d01389056c2b9eb60a95017db0e34f))
- `2026-05-22` — [PR3 — self-upgrade apply + rollback + escape hatch (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-pr3-apply-rollback.md) ([`28ed94a`](https://github.com/dotnetpower/elb-dashboard/commit/28ed94abbadc0b2a7ff80ae3d47bf258ccb21059))
- `2026-05-22` — [PR4 — self-upgrade UX + history (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-pr4-ux-history.md) ([`6679940`](https://github.com/dotnetpower/elb-dashboard/commit/6679940fa9de13f94eececd77daa6f57d65dfed9))
- `2026-05-22` — [F2 — ACR retention pre-flight for rollback (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-f2-acr-retention-preflight.md) ([`bc3f553`](https://github.com/dotnetpower/elb-dashboard/commit/bc3f55310792504ae13ff540a2837512d096941c))
- `2026-05-22` — [BLAST DB hardening round 2 — concurrency, signatures, security, cancel UX](../features_change/2026-05/2026-05-22-blast-db-hardening-round2.md) ([`409d02f`](https://github.com/dotnetpower/elb-dashboard/commit/409d02f21a0d18c1a9da462276b1b90d396733a0))

## Fixes

- `2026-05-22` — [F1 — `__version__` auto-injection (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-f1-version-auto-injection.md) ([`3a1b2e4`](https://github.com/dotnetpower/elb-dashboard/commit/3a1b2e48a25f330d995c571f9e1de38400732363))
- `2026-05-22` — [BLAST submit — stop consuming retry budget on lock contention](../features_change/2026-05/2026-05-22-submit-lock-requeue.md) ([`68dc696`](https://github.com/dotnetpower/elb-dashboard/commit/68dc696ef1b10134eb469a344f2b36460856ad35))

## Other

- `2026-05-23` — [K8s pooled session — bump HTTPAdapter pool_maxsize to 32](../features_change/2026-05/2026-05-23-k8s-session-pool-size.md) ([`ff630d4`](https://github.com/dotnetpower/elb-dashboard/commit/ff630d45483dc3319b25f8a1a39d6f8046293aa4))
- `2026-05-23` — [monitor_cache — JSON bytes storage (no more deepcopy)](../features_change/2026-05/2026-05-23-monitor-cache-json-bytes.md) ([`b04657a`](https://github.com/dotnetpower/elb-dashboard/commit/b04657a4548dce0152652af45d954a52a6c00d64))
- `2026-05-23` — [frontend_proxy — stream the upstream response instead of buffering](../features_change/2026-05/2026-05-23-frontend-proxy-streaming.md) ([`87dff03`](https://github.com/dotnetpower/elb-dashboard/commit/87dff031a3cbfe3894156cddbeacc1e2c7e58487))
- `2026-05-23` — [audit_log — collapse N+1 history query into a single bulk read](../features_change/2026-05/2026-05-23-audit-log-bulk-history.md) ([`40f883a`](https://github.com/dotnetpower/elb-dashboard/commit/40f883ad8d1ab5a51571fe907bee93c5650a4a24))
- `2026-05-23` — [require_caller — async + lazy threadpool offload](../features_change/2026-05/2026-05-23-async-require-caller.md) ([`b451f08`](https://github.com/dotnetpower/elb-dashboard/commit/b451f08d2e5cd7377608ec3b7c9655f64e721a14))
- `2026-05-23` — [wait_for_warmup_jobs — dedup state writes + adaptive poll backoff](../features_change/2026-05/2026-05-23-warmup-polling-backoff.md) ([`b31c022`](https://github.com/dotnetpower/elb-dashboard/commit/b31c0228b3582d0eb9d3924a234072aca99d4d7a))
- `2026-05-23` — [storage_usage_cache — JSON bytes storage, no per-hit deepcopy](../features_change/2026-05/2026-05-23-storage-usage-cache-json-bytes.md) ([`20b53de`](https://github.com/dotnetpower/elb-dashboard/commit/20b53de1268b6a2f028cbc5068016138460d37f4))
- `2026-05-23` — [sanitise — short-circuit + factored GUID redactor](../features_change/2026-05/2026-05-23-sanitise-short-circuit.md) ([`33d64fa`](https://github.com/dotnetpower/elb-dashboard/commit/33d64fa41761d7ec8d439c0a66a644c7e602f25e))
- `2026-05-23` — [request-detail inspector — lazy slice (drop the duplicate body buffer)](../features_change/2026-05/2026-05-23-inspector-body-dedup.md) ([`5a86e02`](https://github.com/dotnetpower/elb-dashboard/commit/5a86e02b97cacb1c2ec4c06c0baf577662195261))
- `2026-05-23` — [JWKS single-flight election](../features_change/2026-05/2026-05-23-jwks-single-flight.md) ([`2683176`](https://github.com/dotnetpower/elb-dashboard/commit/2683176247cf17c61beb165a46bd334e48dab3f9))
- `2026-05-23` — [_ensure_table — double-checked lock to collapse first-boot herd](../features_change/2026-05/2026-05-23-ensure-table-double-check-lock.md) ([`84c3a35`](https://github.com/dotnetpower/elb-dashboard/commit/84c3a35cafaff134a7ecec16f1ed03263b319ea4))
- `2026-05-23` — [k8s_monitoring — shared ThreadPoolExecutor (drop per-call spawn)](../features_change/2026-05/2026-05-23-k8s-shared-threadpool.md) ([`874f5d9`](https://github.com/dotnetpower/elb-dashboard/commit/874f5d9264fcaa2656412fd4f35eb4460b42d153))
- `2026-05-23` — [lifespan — warm DefaultAzureCredential at startup](../features_change/2026-05/2026-05-23-credential-warmup.md) ([`d7e4a37`](https://github.com/dotnetpower/elb-dashboard/commit/d7e4a375e9eecb45381fe9ae84d13f097cbda3b3))
- `2026-05-23` — [_shard_set_already_present — single list_blobs probe](../features_change/2026-05/2026-05-23-shard-set-batched-probe.md) ([`6fb6dec`](https://github.com/dotnetpower/elb-dashboard/commit/6fb6dec71c9975feae7c247d224e19e927b5cc84))
- `2026-05-23` — [cancel — raise the child-limit cap and reject overflow explicitly](../features_change/2026-05/2026-05-23-cancel-overflow-guard.md) ([`5300bbe`](https://github.com/dotnetpower/elb-dashboard/commit/5300bbe01df6501da34fbf4d4eb2944aef87a796))
- `2026-05-23` — [Tail-batch P2 — lifecycle + concurrency + streaming proxy + lock-free emit](../features_change/2026-05/2026-05-23-tail-batch-p2.md) ([`575663a`](https://github.com/dotnetpower/elb-dashboard/commit/575663a7c4a35b57af8418e262064ed4dfb5806e))
- `2026-05-23` — [ACR card: surface in-progress builds after browser refresh](../features_change/2026-05/2026-05-23-acr-card-pending-build-rows.md) ([`a7031aa`](https://github.com/dotnetpower/elb-dashboard/commit/a7031aa1c22ba36d5f2313a150e7d22ad8332a3a))
- `2026-05-23` — [BLAST DB catalog: mark unsupported entries with a dedicated badge](../features_change/2026-05/2026-05-23-blast-db-catalog-unsupported-flag.md) ([`a7031aa`](https://github.com/dotnetpower/elb-dashboard/commit/a7031aa1c22ba36d5f2313a150e7d22ad8332a3a))
- `2026-05-23` — [Dashboard cards: minimum shimmer duration on refresh](../features_change/2026-05/2026-05-23-monitor-card-min-shimmer.md) ([`a7031aa`](https://github.com/dotnetpower/elb-dashboard/commit/a7031aa1c22ba36d5f2313a150e7d22ad8332a3a))
- `2026-05-22` — [In-app self-upgrade — design (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-design.md) ([`6c24603`](https://github.com/dotnetpower/elb-dashboard/commit/6c246037864a120d906a30ec5f38c463f2eb6743))
- `2026-05-22` — [Self-upgrade — 20-point critique hardening (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-critique-hardening.md) ([`bc34a7e`](https://github.com/dotnetpower/elb-dashboard/commit/bc34a7e3eae6731708b6a369ad92946466faa702))
- `2026-05-22` — [Self-upgrade — 40-point critique hardening (2026-05-22)](../features_change/2026-05/2026-05-22-self-upgrade-critique-40-hardening.md) ([`5fa611f`](https://github.com/dotnetpower/elb-dashboard/commit/5fa611f5987832f7d5a760fe33b35c2bc19aa243))
- `2026-05-22` — [Redis client pool — stop per-call `from_url` leak](../features_change/2026-05/2026-05-22-redis-client-pool.md) ([`ebc6011`](https://github.com/dotnetpower/elb-dashboard/commit/ebc60115b871f73f160a0b36ac27c428aa713308))
- `2026-05-22` — [Split-parent XML merge — streaming rewrite](../features_change/2026-05/2026-05-22-split-xml-merge-streaming.md) ([`8314071`](https://github.com/dotnetpower/elb-dashboard/commit/831407154158d2425d420d123e4d4478e7b2ecf8))
- `2026-05-22` — [Bound every metadata `download_blob().readall()` with a hard size cap](../features_change/2026-05/2026-05-22-metadata-readall-cap.md) ([`2b354de`](https://github.com/dotnetpower/elb-dashboard/commit/2b354de1f20c23062b3b92312962d03e44b5e195))
- `2026-05-22` — [Bound worker memory: exec_server output cap + Celery lifecycle limits](../features_change/2026-05/2026-05-22-exec-and-celery-bounds.md) ([`00126cc`](https://github.com/dotnetpower/elb-dashboard/commit/00126ccf1aca590f0b36500adcc495cce0901434))
- `2026-05-22` — [Parallelize split-child report + artifact downloads](../features_change/2026-05/2026-05-22-split-merge-parallel-fanout.md) ([`3f5668a`](https://github.com/dotnetpower/elb-dashboard/commit/3f5668aa9d38147236fbc399d08a2642c235e6c8))
- `2026-05-22` — [BLAST XML parser — incremental walk via iterparse](../features_change/2026-05/2026-05-22-blast-xml-iterparse.md) ([`61e9c66`](https://github.com/dotnetpower/elb-dashboard/commit/61e9c6660c97451c1ad4788676804883508f5c1a))
- `2026-05-22` — [Cache hot path: drop deepcopy + use OrderedDict LRU eviction](../features_change/2026-05/2026-05-22-cache-deepcopy-ordereddict.md) ([`4d2e6c8`](https://github.com/dotnetpower/elb-dashboard/commit/4d2e6c8977f8cea43042fbed55e3bece1886417f))
- `2026-05-22` — [Poll cadence — back off after the first minute](../features_change/2026-05/2026-05-22-blast-poll-backoff.md) ([`28d3ba6`](https://github.com/dotnetpower/elb-dashboard/commit/28d3ba66484a66b038eac1d54f45bb9dd5d13d49))
- `2026-05-22` — [Pool the per-request TableClient in job_artifacts + auto_warmup](../features_change/2026-05/2026-05-22-table-client-pool.md) ([`6687035`](https://github.com/dotnetpower/elb-dashboard/commit/6687035352236fa06f1a2904fd7b22055c774c76))
- `2026-05-22` — [BlobServiceClient pool — finalizer + idle TTL](../features_change/2026-05/2026-05-22-blob-pool-finalizer-ttl.md) ([`fcf6398`](https://github.com/dotnetpower/elb-dashboard/commit/fcf63989249a4ffa1b874d990be3cbc4a32e699e))
- `2026-05-22` — [exec_server: line-length cap + temp-dir GC daemon](../features_change/2026-05/2026-05-22-exec-line-cap-tmpdir-gc.md) ([`50f4e90`](https://github.com/dotnetpower/elb-dashboard/commit/50f4e90469870aa6588e6d23b7ed24157c912db1))
- `2026-05-22` — [stream_blob_bytes — wrap every active transfer in a bounded semaphore](../features_change/2026-05/2026-05-22-storage-stream-semaphore.md) ([`92b739a`](https://github.com/dotnetpower/elb-dashboard/commit/92b739ae1879fdb6aa1f846d313e02294dbfd3df))
- `2026-05-22` — [Tail hardening: inflight TTL + event_emitter shutdown + blob fast path](../features_change/2026-05/2026-05-22-tail-hardening-batch.md) ([`b54c4f5`](https://github.com/dotnetpower/elb-dashboard/commit/b54c4f5855a6c93e924d0e4faf84beab71861bea))
- `2026-05-22` — [reset_credential() cascade — reset every downstream pool](../features_change/2026-05/2026-05-22-reset-credential-cascade.md) ([`f2e7507`](https://github.com/dotnetpower/elb-dashboard/commit/f2e750742afc342ef21d8d68a8b50bfccedf2652))
- `2026-05-22` — [2026-05-22 — `/api/arm/.../locations` ModuleNotFoundError fix](../features_change/2026-05/2026-05-22-arm-locations-import-fix.md) ([`a7031aa`](https://github.com/dotnetpower/elb-dashboard/commit/a7031aa1c22ba36d5f2313a150e7d22ad8332a3a))
- `2026-05-22` — [2026-05-22 — `local-debug-auth.sh` one-shot toggle for real MSAL login locally](../features_change/2026-05/2026-05-22-local-debug-auth-toggle.md) ([`a7031aa`](https://github.com/dotnetpower/elb-dashboard/commit/a7031aa1c22ba36d5f2313a150e7d22ad8332a3a))
