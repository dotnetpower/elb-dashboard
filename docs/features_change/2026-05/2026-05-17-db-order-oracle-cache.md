# DB Order Oracle Cache

## Motivation

Positive-hit Web BLAST equivalence is blocked by top-N tied-hit selection at the `max_target_seqs` boundary. A query-specific strict top-N oracle can prove equality, but it is too expensive to discover during every BLAST submit. BLAST submit must stay fast.

## User-facing change

- The BLAST database manager now exposes an order-oracle build action for warmed databases.
- The action is intended to run when a DB snapshot changes, or when the user explicitly clicks the build button.
- Precise sharded BLAST submits no longer need to generate oracle data on the search path. When a cached DB-order oracle exists, submit uploads a small pointer manifest for the finalizer.
- Database rows surface cached oracle status and ready part counts.
- Warmup status now detects stale completed warmup Jobs that are pinned to nodes that disappeared after an AKS stop/start cycle.

## API and runtime diff

- Added `POST /api/blast/databases/{db_name}/oracle` to create one Kubernetes Job per warmed shard.
- Added `api/services/db_order_oracle.py` for stable oracle status/part paths and Kubernetes Job manifest generation.
- `GET /api/blast/databases` now includes `db_order_oracle` metadata from `blast-db/metadata/oracles/<db>/status.json` and part blobs.
- `api/tasks/blast.py` now attaches `metadata/tie-order-oracle-urls.txt` for precise sharded submits when cached DB-order oracle parts are available.
- `terminal/patch_elastic_blast.py` now patches the finalizer to download DB-order oracle part URLs, concatenate them in part order, and export `ELB_TIE_ORDER_FILE`.
- `api/services/warmup_jobs.py` now reports shard node names and host paths; `api/services/k8s_monitoring.py` marks warmup as `Stale` when completed Jobs target nodes that are no longer Ready.

## Validation evidence

- Focused backend tests: `uv run pytest -q api/tests/test_db_order_oracle.py api/tests/test_warmup_jobs.py api/tests/test_storage_data.py api/tests/test_blast_tasks.py` -> `111 passed`.
- Full backend tests: `uv run pytest -q api/tests` -> `604 passed`.
- Backend lint: `uv run ruff check api` -> `All checks passed!`.
- Frontend build previously passed for the UI button/status change; rerun pending after final live probe.
- Live AKS check: cluster `elb-cluster` is `Succeeded` / `Running`.
- Live warmup remediation: existing `core_nt` warmup Jobs were stale after AKS restart because they targeted removed nodes (`...00a` through `...00j`). They were released and recreated on current Ready nodes (`...00u` through `...013`). Backend warmup status now reports `core_nt` as `Ready` with `10/10` completed shards.
- Live network remediation: `elbstg01.blob.core.windows.net` initially resolved to public IP `20.150.4.36` inside AKS while `publicNetworkAccess` was `Disabled`, causing warmup `AuthorizationFailure`. Created blob private endpoint `pe-elbstg01-blob`, private DNS zone `privatelink.blob.core.windows.net`, VNet link, and DNS zone group. AKS now resolves `elbstg01.blob.core.windows.net` to private IP `10.224.0.15`.
- Live oracle build: created 10 DB-order oracle Jobs for `core_nt` run `20260517164853-89081927`. All 10 completed and uploaded parts under `blast-db/metadata/oracles/core_nt/parts/20260517164853-89081927/`.
- Uploaded `blast-db/metadata/oracles/core_nt/status.json` from inside AKS with `status=ready`, `expected_parts=10`, and `ready_parts=10`.
- Per-shard oracle completion counts: `00=14215475`, `01=14220903`, `02=14110105`, `03=13988357`, `04=14146638`, `05=14312905`, `06=14380566`, `07=14053353`, `08=14232474`, `09=10285005` accessions.

## Residual risk

- DB-order oracle is a production-friendly cached tie-breaker, not yet a proven replacement for a query-specific strict top-N membership oracle on F3L/core_nt. The next live proof is to run a positive-hit precise sharded submit with the cached pointer and compare against same-snapshot Web/full-run evidence.
- The live `rg-elb-01` workload resources are older than the Container Apps IaC target. The private endpoint repair was applied directly to the running environment; the active `rg-elb-ca` IaC already contains private endpoints for its managed storage account.
