---
title: OpenAPI Storage convergence and Direct progress
description: Prevent empty OpenAPI Storage targets during fast deployments and show live NCBI Direct archive progress from the pending generation.
tags: [blast, operate, ui]
---

# OpenAPI Storage convergence and Direct progress

## Motivation

A live [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) fast deployment preserved an empty `STORAGE_ACCOUNT_NAME` on the API, worker, beat, and terminal sidecars. The next automatic [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) OpenAPI deployment propagated that empty value as `ELB_STORAGE_ACCOUNT`, so every metadata read and query upload addressed `https://.blob.core.windows.net` and timed out. The same live run showed the database manager rendering the previous completed S3 copy count (`731 / 731 files`) while a new NCBI Direct generation was actually downloading `0 / 85 archives`.

## User-facing change

- Fast deployments restore the non-secret Storage account coordinate on every Bicep-owned runtime sidecar.
- OpenAPI automatic, manual, task, and manifest boundaries refuse to deploy with an empty Storage account instead of creating a broken workload.
- Manual OpenAPI deploys recover older UI payloads from the platform Storage environment.
- Active NCBI Direct updates render pending-generation archive progress rather than stale progress from the currently active S3 generation.
- After a worker revision replacement, the orphan reconciler refreshes active Direct archive counters from the surviving indexed AKS Job, so progress remains live instead of freezing until promotion.
- The confirmation distinguishes NCBI's database-volume count from the additional pinned taxonomy bundle, explaining why `core_nt` shows 84 database archives but the indexed Job tracks 85 total archives.
- The target deployment enables Auto oracle execution with current-caller RBAC revalidation. The existing enabled preference is scoped to `core_nt` on `elb-cluster-01` only.
- External/Service Bus result downloads combine the captured relative blob path with the job's durable flat or date-tiered results prefix. Missing blobs fall back to the live OpenAPI stream instead of escaping the response as HTTP 500.
- Background result manifests include only parseable BLAST output files; Storage directory markers, metadata, and runtime logs no longer appear in the user result list.
- Pod-log persistence preserves the external job's trusted canonical columns while merging nested progress payloads. It no longer clears the Storage account or degrades `blastn - core_nt` to `blast`, so the same finalizer pass can build result artifacts.
- Redis drain-lock disconnects during a sidecar revision replacement still fail closed and rely on the lease TTL, but log the exception type without traceback telemetry so expected coordination recovery no longer appears as an App Insights exception.
- Failed Auto warm retries use Foreground deletion and wait up to 120 seconds for current Jobs/pods and legacy DaemonSets/pods to disappear before recreating same-name resources. A classified delete, verification, or timeout failure returns `partial` and blocks recreation, preventing terminating old pods from overlapping a retry on the same node-local path. Manual release remains non-blocking for compatibility.
- Warmup cache reuse and completion now require a full `blastdbcmd -entry all` record-stream probe in addition to `blastdbcmd -info`. A stale-generation file that leaves metadata readable but corrupts record access invalidates the marker and fails the shard instead of allowing Auto oracle or BLAST to discover it later.
- Oracle terminal failure captures the first failed Job's final 20 log lines before cleanup, sanitises them, and applies the existing 300-character durable error cap. Diagnostic retrieval is best-effort and bounded to one Job, so an unavailable log endpoint cannot prevent cleanup or terminal state.
- Monitor snapshot refreshes classify the repository's open AKS circuit-breaker signal as transient. Repeated `ClusterApiUnreachable` stale-cache fallbacks retain one diagnostic stack per dedup window and a counter for every failure without creating an App Insights exception row every five-second poll.

## API, deployment, and UI summary

- `scripts/dev/quick-deploy.sh` upserts `STORAGE_ACCOUNT_NAME` from the validated azd target on API, worker, beat, and terminal exact-container patches. An empty local value remains a no-op and cannot erase a live coordinate.
- `build_auto_openapi_payload` skips enqueue when no Storage account can be resolved.
- The manual deploy route resolves Storage account and resource group fallbacks and returns `missing_storage_account` before enqueue when no target exists.
- `deploy_openapi_service` and `build_manifests` fail before Azure or Kubernetes mutation when the Storage account is empty.
- `BlastDbRow` prefers `pending_generation.succeeded_archives / archive_count` for an active `ncbi-direct` generation; existing server-copy and AKS-fanout progress contracts are unchanged.
- The orphan reconciler updates only active Direct counters after revalidating operation owner, generation ID, and Job name under Blob ETag CAS. A concurrent replacement operation wins without being overwritten.
- The offline result streamer validates the resolved job-owned path before opening Storage, avoids double-prefixing already container-relative paths, and converts only `BlobNotFound` into the existing OpenAPI fallback signal. Authorization and network failures remain visible.
- No Storage network rule, SAS flow, Azure resource, role assignment, Service Bus target, or dependency changed.

## Validation evidence

- Focused Direct update, ownership, recovery, readiness, Auto warm, and Auto oracle baseline: `169 passed`.
- Quick-deploy coordinate regression: `30 passed` before the wider boundary fix.
- OpenAPI Storage boundary regression suite: `117 passed`.
- Direct progress and confirmation component regression: `10 passed`.
- Live OpenAPI repair task completed in 39 seconds with one ready replica and `ELB_STORAGE_ACCOUNT=stelbdashboardcyutlgcnv3`; subsequent Service Bus submission returned OpenAPI HTTP 202.
- Live `core_nt` Direct update started at `2026-08-29T15:41:38.840Z`: generation `ncbi-direct-20260819-cab30d18c360`, task `03fb194f-c3fc-4801-b1ce-5e6422f4e848`, 85 indexed completions, parallelism 4, and zero initial failures.
- The indexed download completed 85/85 archives with zero failures at `2026-08-29T17:36:36Z`, taking 6,889 seconds (1 hour 54 minutes 49 seconds). The post-grace reconciler validated and promoted 766 files / 295,616,990,673 bytes at `2026-08-29T17:43:21.770793Z`; click-to-promotion was 2 hours 1 minute 43 seconds. The previous generation stayed active until that atomic commit, and the Direct Job, ConfigMap, and pods were then removed.
- The promoted generation exposes all shard layouts `[1, 2, 3, 4, 5, 6, 8, 10]`, `shard_source_version` equals the new generation, `shards_stale=false`, and the old oracle is explicitly `stale` rather than silently reused.
- A uniquely identified Service Bus probe was enqueued at `2026-08-29T15:42:51Z` and accepted as OpenAPI job `c1b89b30473f` at `2026-08-29T15:43:18Z` while the Direct update was active (27 seconds enqueue-to-acceptance). Its ConfigMap pinned the previous active database version. It ran from `2026-08-29T16:21:12.747364Z` to `2026-08-29T16:24:24.386169Z` (about 3 minutes 12 seconds runtime; about 41 minutes 33 seconds enqueue-to-completion under the existing backlog) and completed all 10/10 BLAST pods.
- The same Service Bus probe exposed a date-layout result download HTTP 500. After repair, both external and dashboard file routes returned HTTP 200 with the expected 396-byte gzip file. Rebuilding its idempotent artifacts reduced the dashboard result list from 69 mixed Storage entries (33 zero-byte markers) to one merged BLAST output with no metadata/log/directory entries.
- Service Bus health after the backlog drained reported active `0`, scheduled `0`, request DLQ `0`, completion pending/DLQ `0`, and outbox pending `0`.
- Auto warm task `c522f574-33ee-4228-b652-c4c14d60bb78` started 51 seconds after promotion. All ten node Jobs target `ncbi-direct-20260819-cab30d18c360`; at the 11-minute checkpoint all were active with zero failures/restarts, node CPU was 38-44%, and node memory was 17-18%. Auto oracle was correctly blocked as `warmup_generation_stale` with zero retry-budget consumption.
- The forced Auto warm retry completed 10/10 Jobs with zero failed Jobs in 23 minutes 48 seconds by Kubernetes Job conditions. The first Auto oracle run and its automatic retry both isolated shard `02`: `blastdbcmd` failed with `Frame type=eFrameClassMember, Member name=title` while the warmup's metadata-only `-info` probe had passed.
- A temporary node-pinned diagnostic proved only `core_nt.21.nhr` was corrupt. The official 2,792,299,029-byte archive matched MD5 `bcabcef7f8895f0541b082481fbe8397` and enumerated successfully. The official member and active immutable Storage blob both had 310,982,213 bytes and SHA-256 `0440277357658dc063cd24da3377681a5752125c621544fc09928dd1bfbb894a`; the node-local file had 315,191,936 bytes and SHA-256 `9ce542274025faccbe31f7541155faceb406ead3a453f173df9d8975c5974cf3`, exactly matching the previous legacy Storage blob. Kubernetes events showed the old warmup pod receiving termination at `18:20:24Z` and its replacement starting at `18:20:36Z`, confirming cross-generation write overlap during background deletion.
- `blastdbcheck` was rejected as the warmup gate after it reported one error for each healthy standalone volume and eight errors for the healthy eight-volume shard alias. After atomically replacing the stale node-local file from the verified active-generation blob, full `blastdbcmd` enumeration succeeded in 38 seconds; the same enumeration had failed in about 30 seconds before repair. The record-stream probe therefore adds a measured integrity gate without relying on the false-positive checker.
- The repaired automatic oracle run `20260829192214-0757dc7b` was dispatched on the first eligible reconcile at `19:22:14Z` and published all 10/10 parts for `ncbi-direct-20260819-cab30d18c360` at `19:23:24Z`. The task completed in 68.61 seconds with zero cleanup errors, and automation reset from two failures to `failure_count=0`, `status=ready`, and no pending retry.
- Click-to-final-oracle elapsed time was about 3 hours 41 minutes 46 seconds, from the Direct update request at `15:41:38.840Z` through oracle publication at `19:23:24.948Z`. This includes the 2 hour 1 minute 43 second atomic update/promotion, the initial Auto warm failure and 23 minute 48 second successful warm retry, two failed oracle attempts, diagnosis, repair, and the 68.61 second successful automatic retry.
- One Service Bus health sample at `19:23:17Z` failed closed with `openapi_pod_check_failed` during concurrent OpenAPI traffic. The Deployment, pod, Service, internal LoadBalancer, and endpoint remained Ready; repeated sibling probes before and after were HTTP 200. A direct in-pod `/v1/ready` check at `19:25:22Z` returned HTTP 200 with `k8s_api=ok`, `openapi_pod=ok`, and `workload_pool=ok` for all 10 nodes, confirming automatic recovery rather than a persistent admission fault.
- The four-hour App Insights review found no request failure after the two pre-fix result-download errors at `16:41Z`, and no application exception after `18:44Z`. It also exposed 22 duplicate monitor-refresh exception rows in 106 seconds while the AKS circuit breaker was open; the transient classifier did not recognise its local `ClusterApiUnreachable` type. The classifier fix preserves graceful stale responses and suppresses only repeated traceback telemetry. Ten create-before-apply oracle Job 404 dependencies and Storage/Table not-found probes were expected idempotent control flow.
- Final full backend suite after the warmup integrity and oracle diagnostic fixes: `5545 passed, 4 skipped`; `uv run ruff check api` passed. Focused warmup cleanup/integrity sweep: `126 passed`. Complete oracle-focused sweep: `140 passed`. Full frontend suite: 109 files / 988 tests and production build passed. Persona/deployment contract sweep: `138 passed`. The focused result pipeline sweep passed 196 tests, and the complete Service Bus task suite passed 134 tests. Documentation frontmatter and strict MkDocs builds passed.
