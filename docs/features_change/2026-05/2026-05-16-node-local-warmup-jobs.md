# 2026-05-16 — Node-local Warmup Job Manifests

## Motivation

`core_nt` warmup on E16 x 10 needs deterministic one-shard-per-node placement,
node-local hostPath storage, partial-download cleanup, and a status model that
can distinguish real node warmup from storage-only sharding metadata.

The existing `/api/warmup/start` flow prepared and sharded the database in
Storage, but did not apply Kubernetes node-local warmup Jobs. This change wires
that runtime step into the existing Celery task while keeping the public route
shape stable for the SPA.

## User-Facing Change

When the SPA starts warmup for a running AKS cluster, the backend now creates
one node-local Kubernetes Job per selected shard and waits until those Jobs
complete. The existing dashboard warmup status can recognise Jobs labelled
`app=elb-db-warmup` and report node readiness from them.

## API / IaC Diff Summary

- Added `api.services.warmup_jobs`:
  - Builds one Kubernetes Job manifest per shard.
  - Pins each shard to a specific AKS node for one-shard-per-node E16 x 10 warmup.
  - Mounts node-local hostPath storage at `/blast/blastdb` so generated `.nal`
    aliases match the path BLAST sees inside the container.
  - Runs `init-db-shard-aks.sh`, verifies no `.azDownload-*` partial files remain,
    verifies nucleotide volume files exist, then runs `blast-vmtouch-aks.sh`.
  - Avoids sourcing `/tmp/shard_volpaths.txt`, which is unsafe for space-separated
    paths and caused an earlier manual shard smoke failure.
- Updated `api.services.k8s_monitoring.k8s_warmup_status` to merge
  `app=elb-db-warmup` Job status into the existing dashboard warmup status shape.
- Added direct Kubernetes API helpers to select Ready blastpool nodes and create
  warmup Jobs idempotently without depending on the browser terminal's `az login`.
- Updated `api.tasks.storage.warmup_database` to run storage verification,
  sharding, node-local Job creation, and warmup completion polling in one Celery
  flow.
- Updated `/api/warmup/start` to pass AKS, ACR, and cluster topology to the task.
- Added unit tests for E16 x 10 manifest placement, unsafe input rejection,
  shard selection, node filtering, idempotent Job creation, route task arguments,
  and warmup Job status aggregation.

## Validation Evidence

```text
uv run pytest -q api/tests/test_warmup_jobs.py api/tests/test_warmup_route.py
12 passed in 1.88s
```

```text
uv run pytest -q api/tests
432 passed in 23.24s
```

```text
uv run ruff check api/services/k8s_monitoring.py api/services/warmup_jobs.py api/tasks/storage.py api/tests/test_warmup_jobs.py api/tests/test_warmup_route.py
All checks passed!
```

```text
uv run ruff check api/services/k8s_monitoring.py api/services/warmup_jobs.py api/tests/test_warmup_jobs.py
All checks passed!
```

```text
python -m py_compile api/services/k8s_monitoring.py api/services/warmup_jobs.py
# passed with no output
```
