---
title: core_nt sharding default + OpenAPI token 401 self-heal on submit/get/list
description: Service Bus and direct OpenAPI BLAST submits now promote a missing/standard resource_profile to a memory-heavy database's sharding default (core_nt → core_nt_safe) so core_nt runs sharded instead of failing the single-node memory-fit check, and the stale-token 401 self-heal that already covered the readiness probe now also covers get_job / list_jobs / submit_job so a token drift never hides the real failure reason.
tags:
  - blast
  - operate
---

# 2026-06-15 — core_nt sharding default + token 401 self-heal on the data path

## Motivation

A Service-Bus-submitted `core_nt` BLAST job failed at the submit step with only
a generic *"OpenAPI service reported no error detail"* banner. Two stacked
defects were behind it:

1. **A stale `X-ELB-API-Token` 401 hid the real error.** The dashboard's
   ephemeral runtime token cache had diverged from the `elb-openapi` pod's
   minted token (a control-plane redeploy wiped the Redis sidecar while the pod
   kept its token). The failure-detail recovery (`external_blast.get_job`) 401'd,
   so the dashboard could not fetch the sibling's real error and fell back to the
   generic banner. The existing 401 self-heal only covered the `/v1/ready` probe.

2. **`core_nt` was submitted unsharded.** Once the token was rotated the real
   error surfaced: *"BLAST database core_nt memory requirements exceed memory
   available on selected machine type Standard_E16s_v5. Please select machine
   type with at least 251.7GB."* core_nt's `bytes_to_cache` (~252 GB) does not
   fit a 128 GB blast-pool node, so it MUST run sharded. The sibling only builds
   a sharded config when the submit carries a sharding-family `resource_profile`
   (`core_nt_precise` / `precise` / `core_nt_safe`), but the Service Bus message
   (and a direct OpenAPI submit that omits the field) defaulted to `standard`.

## User-facing change

* **core_nt now runs sharded by default.** Both submit paths
  (`POST /api/v1/elastic-blast/submit` and the Service Bus drain → OpenAPI
  bridge) apply a server-derived default: a database that must run sharded
  (today `core_nt`) with a missing or `standard` `resource_profile` is promoted
  to its sharding default (`core_nt_safe`). An explicit sharding-family profile,
  or any non-`standard` caller value, is preserved unchanged. Small databases
  (e.g. `16S_ribosomal_RNA`) keep `standard` — they are never promoted. This
  matches the dashboard's existing catalogue / API-Reference pairing of core_nt
  with `core_nt_safe`; the prefix convention (`blast-db/{N}shards/core_nt_shard_`)
  is identical across the dashboard warmup, the sibling derivation, and the
  sibling's core_nt branch, and the 3-shard layout is already staged in storage.
* **Token drift no longer hides the failure reason.** The stale-token 401
  self-heal (re-read the live token from the cluster, sync it into the runtime
  cache, retry once) now wraps `get_job`, `list_jobs`, and `submit_job` — not
  just the readiness probe. A token mismatch self-heals on the next data-path
  call instead of surfacing a spurious auth error (and, for the recovery path,
  the real BLAST failure reason instead of the generic "no error detail").

## API / IaC diff summary

* New pure helper `resolve_sharded_db_resource_profile(database, requested)` in
  [api/services/blast/submit_payload.py](../../../api/services/blast/submit_payload.py),
  applied in [api/routes/elastic_blast.py](../../../api/routes/elastic_blast.py)
  (direct submit) and
  [api/tasks/servicebus/tasks.py](../../../api/tasks/servicebus/tasks.py)
  (`_build_request_payload`). No request/response schema change — only the
  server-derived `resource_profile` forwarded to the sibling.
* New helper `_request_with_token_resync(...)` + `_resync_token_after_401()` in
  [api/services/external_blast.py](../../../api/services/external_blast.py);
  `get_job` / `list_jobs` / `submit_job` route their HTTP call through it. The
  submit retry reuses the same `idempotency_key`, so a self-heal retry cannot
  create a duplicate cluster job.
* **No sibling change and no image rebuild** — the sibling
  (`elastic-blast-azure`) already shards core_nt for the sharding-family
  profiles; this only makes the dashboard send the right profile. Worker (+ api)
  redeploy carries the change.

## Operational note

The live deployment's stuck job was recovered out-of-band by rotating the token
(`POST /api/aks/openapi/token {regenerate:true}` for `elb-cluster-01`), which
re-syncs the pod + dashboard to the same value. The self-heal change makes that
recovery automatic for future drift.

## Validation evidence

* Backend: `uv run pytest -q api/tests` — **3684 passed, 3 skipped**. New focused
  suite `test_sharded_db_profile.py` (helper matrix + both submit paths);
  token self-heal unit tests in `test_external_blast_api.py`;
  `test_servicebus_tasks.py` updated to assert the core_nt → core_nt_safe
  promotion.
* Lint: `uv run ruff check api` — clean.
* Live: App Insights confirmed the pre-fix `openapi_http_401` chain on job
  `981f74c3d130`; after token rotation the real error
  (`memory requirements exceed … 251.7GB`) was retrievable, confirming the
  diagnosis the sharding default addresses.
