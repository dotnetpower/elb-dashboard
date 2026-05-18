# OpenAPI core_nt precise sharding

## Motivation

The external ElasticBLAST OpenAPI execution plane needed to submit `core_nt` using the same local-SSD precise sharding policy as the dashboard path. Before this change the OpenAPI image could accept a request, but it could not reliably build the patched ElasticBLAST runtime, create the `elb-scripts` ConfigMap, or emit the 10-shard `core_nt` config.

## User-facing change

OpenAPI `resource_profile=core_nt_precise` submissions now run with local SSD, 10 `core_nt` partitions, and the Web BLAST-compatible `searchsp` value. The OpenAPI proxy client can send `X-ELB-API-Token` when `ELB_OPENAPI_API_TOKEN` is configured.

## API / runtime diff summary

- Added `scripts/dev/patch-openapi-build-context.py` so the sibling `docker-openapi` context can be patched reproducibly before ACR build.
- The patch installs the dashboard ElasticBLAST runtime template fixes into both the OpenAPI app runtime and CLI runtime package paths.
- The patch adds OpenAPI app handling for `resource_profile=core_nt_precise`:
  - `cluster.exp-use-local-ssd=true`
  - `cluster.reuse=true`
  - `blast.db-partitions=10`
  - `blast.db-partition-prefix=https://<account>.blob.core.windows.net/blast-db/10shards/core_nt_shard_`
  - `-searchsp 32156241807668` when no explicit `-searchsp`/`-dbsize` is present
- OpenAPI deployment manifests set `ELB_NUM_NODES` and `ELB_CORE_NT_SHARDS` but no longer inject `AZURE_CLIENT_ID` directly; the AKS Workload Identity webhook owns that env when available.
- `api.services.external_blast` now sends `X-ELB-API-Token` from `ELB_OPENAPI_API_TOKEN`.

## Validation evidence

- Patched and pushed ACR image `elbacr01.azurecr.io/elb-openapi:4.9` digest `sha256:6be57c819a5591651dceb06b544db3e0188a70f0e12fc318ed67a8acd08de0f5` from ACR build run `de1k`.
- Rolled out `deployment/elb-openapi` on AKS and verified runtime package scripts exist in `/usr/local/lib/python3.11/site-packages/elastic_blast/templates/scripts`.
- Verified the rolled pod has job-scoped setup cleanup: `delete jobs -l app=setup,elb-job-id={cfg.azure.elb_job_id}`.
- Submitted authenticated OpenAPI request with `resource_profile=core_nt_precise`; final job id `1f600a7b8e51`.
- Verified persisted config included `num-nodes = 10`, `exp-use-local-ssd = true`, `db-partitions = 10`, and `-searchsp 32156241807668`.
- Verified Kubernetes execution:
  - 10 local SSD init jobs completed (`init-ssd-ea9e875a-0` through `init-ssd-ea9e875a-9`).
  - 10 BLAST shard jobs completed (`blastn-batch-s00-job-000-ea9e875a` through `blastn-batch-s09-job-000-ea9e875a`).
  - Finalizer completed (`elb-finalizer-ea9e875a`).
- Verified OpenAPI status endpoint returned `status: completed`, `phase: completed`, `shards_succeeded: 80`, `shards_failed: 0`.
- Verified OpenAPI job response returned `status: success` with 10 result files.
- Verified result file download endpoint returned gzip XML; `result-001` was `143K` and began with BLAST XML for `core_nt_shard_01`.
