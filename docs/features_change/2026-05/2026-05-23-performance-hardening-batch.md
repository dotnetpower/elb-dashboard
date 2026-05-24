# Performance hardening batch

## Motivation

The May 23 performance audit found hot-path bottlenecks across Storage copy
polling, ARM discovery, frontend polling/render loops, Container App sidecar
resources, Redis broker safety, and build output size. The highest-risk items
were all recurring costs: blob-per-file status probes during prepare-db,
uncompressed and source-mapped frontend assets, undersized sidecars, and
periodic workers competing with user-triggered tasks.

## User-facing change

The dashboard should load faster, reduce background browser work, and stay more
responsive while BLAST database preparation, ACR build monitoring, and warmup
orchestration are active. No API response schema or user workflow changed.

## Code-level diff summary

1. **Storage prepare-db copy polling** now reads copy metadata from one
   prefixed `list_blobs(..., include=["copy"])` call per poll sweep instead of
   issuing `get_blob_properties()` for every staged blob. Cancel now uses the
   same listed copy metadata to avoid another per-blob status roundtrip.
2. **ACR monitoring** caps the run history scan with `ACR_RUNS_LIST_LIMIT`
   (default `100`) so long-lived registries do not make dashboard refreshes
   proportional to all historical runs.
3. **Health and ARM discovery** avoid full subscription/RG sweeps on hot
   diagnostics. Health caps resource-group enumeration at two rows, while
   storage/ACR discovery routes cache same-RG responses for 60 seconds.
4. **Auth validation** caches JWKS clients for 12 hours by default
   (`AUTH_JWKS_TTL_SECONDS`) while keeping the existing short-lived validated
   claims cache.
5. **Celery runtime** increases default worker concurrency, makes prefetch
   env-tunable with a default of four, and staggers the formerly aligned beat
   reconcilers to reduce queue starvation.
6. **Storage downloads** raise the default sidecar streaming semaphore from 4
   to 8 concurrent transfers (`STORAGE_STREAM_MAX_CONCURRENCY`) and extend the
   acquire timeout to 60 seconds.
7. **Frontend polling/rendering** disables production sourcemaps by default,
   adds bounded token-refresh backoff, stops warmup orchestration polling after
   terminal states, pauses timer-driven re-renders in hidden tabs, and uses
   stable keys for BLAST hit/alignment rows.
   Mock sidecar log generation also pauses in hidden tabs, OpenAPI prefetch
   failures are debug-visible in development, and mobile light-theme glass blur
   is reduced.
8. **[Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview)**
   sidecars now allocate 1 vCPU to API and worker containers and protect the
   Redis sidecar with `maxmemory` and an LRU eviction policy.
9. **Frontend nginx** enables gzip for JavaScript, CSS, JSON, text, SVG, and
   WASM assets.
10. **CI** keys the uv cache explicitly on `uv.lock`.

## API / IaC diff

* No API route or response schema changes.
* `infra/modules/containerAppControl.bicep` changes API/worker CPU allocation
   and Redis command-line arguments.
* `web/nginx.conf` enables gzip compression inside the frontend sidecar.

## Validation evidence

* `uv run pytest -q api/tests` — **1371 passed**.
* `uv run ruff check` on all touched Python files — **clean**.
* `cd web && npm run build` — **passed**, production bundle emitted without
  sourcemap artifacts by default.
* `az bicep build --file infra/main.bicep --stdout >/tmp/elb-main-bicep-build.json`
   — **passed** with only the Azure CLI Bicep version-upgrade warning.
* `azd provision --preview` — **passed** in 25 seconds, no deployment applied.
   The preview also surfaced unrelated existing drift: the current template would
   change Key Vault `publicNetworkAccess` from `Disabled` to `enabled` with
   permissive network ACLs. That drift is outside this performance batch and
   should be reviewed before any provisioning run.
