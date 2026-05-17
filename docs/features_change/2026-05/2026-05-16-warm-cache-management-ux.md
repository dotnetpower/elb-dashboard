# Warm Cache Management UX

## Motivation

DB warmup is a node-cache management workflow, not just a submit checkbox. Users need to see whether a downloaded/sharded database is already warm on AKS nodes, whether the current node memory headroom can support warmup, and whether a cache can be released when memory pressure or workload priorities change.

## User-Facing Change

The AKS detail warmup panel now renders a per-database warm cache table instead of a single database selector. Each row shows storage state, shard state, warm-cache state, per-node warmup fit, and direct actions for Warm, Rewarm, or Release when applicable. The panel also shows current warm-cache capacity from node memory metrics.

The compact AKS card database strip now shows only actual node warm-cache state. Downloaded-only and sharded-only databases are hidden there; when no DB is warm, the card prompts the user to enable Auto warm in the BLAST Databases modal or open Details to warm a DB immediately. If a DB is warming, the strip shows the node progress instead of a sharded/downloaded chip.

Warmup progress now carries timing metadata from the Kubernetes warmup Jobs. While shards are active, the detail panel shows percent complete, elapsed time, and an estimated remaining time once at least one shard has finished. The compact AKS database strip also includes a short ETA in the warming chip and the full elapsed/remaining detail in the tooltip.

Warmup progress is now phase-aware instead of only Job-count-aware. The backend tails warmup pod logs and reports whether each shard is waiting for an image pull, copying shard files to node-local disk with azcopy, validating BLAST database files, touching files into RAM, completed, or failed. The compact database strip and detail panel surface the active phase, phase counts, current message, elapsed time, and ETA, so a long 0%/90% warmup no longer looks opaque.

The warmup bootstrap path now ensures the `elb-scripts` ConfigMap before creating warmup Jobs, grants the AKS kubelet identity both AcrPull and Storage Blob Data Reader when permissions allow it, and cleans stale `.azDownload-*` partial directories between retries. This makes warmup Jobs recover cleanly from image-pull, Storage authorization, and interrupted azcopy-copy states.

The BLAST Databases modal now includes an Auto warm preference per database. `core_nt` is selected by default. Checked databases are stored server-side and warmed by Celery when the AKS workload cluster becomes ready/running, provided the DB is downloaded and not already warming or warm.

Auto warm no longer depends on an open dashboard tab. Dashboard Start passes the current Auto warm preference into the AKS start Celery task, which stores the preference and queues reconciliation after AKS reports started. Celery beat also runs the same reconciliation periodically, so a cluster started from Azure Portal can still trigger warmup after the backend observes it as workload-ready.

When an AKS cluster is stopped, starting, stopping, provisioning, or otherwise not workload-ready, the compact cluster card now shows a concise lifecycle readiness panel instead of the submit/runtime bento. The panel reports the current lifecycle state, next automatic refresh behavior, and static cluster summary without showing misleading `Down`, submit counts, live activity, or node-metrics rows.

## API / IaC Diff Summary

- Added `POST /api/warmup/release` to release Kubernetes warmup resources for a selected database.
- Added a direct Kubernetes helper that deletes node-local warmup Jobs and legacy warmup DaemonSets by DB label.
- Extended the frontend monitoring client with `releaseWarmup` and optional warmup shard/status fields.
- Added server-side Auto warm preferences (`PUT/GET /api/warmup/auto-preference`) with Azure Table Storage persistence and a local file fallback for dev.
- Added `api.tasks.storage.reconcile_auto_warmup`, a Celery task that polls AKS/Kubernetes/Storage state and queues idempotent `warmup_database` tasks for configured DBs.
- Added a Celery beat schedule that runs Auto warm reconciliation on the `storage` queue every 60 seconds.
- Extended the AKS Start action so the dashboard passes Auto warm context to the `start_aks` Celery task; after the cluster starts, the task stores the preference and queues reconciliation.
- Replaced the frontend-only cluster-transition warmup trigger with preference sync to the backend.
- Added a non-workload-ready readiness branch in the compact cluster card and suppresses deep operational details until AKS is workload-ready.
- Fixed `/api/warmup/start` to publish through the configured Celery app with the explicit `storage` queue, avoiding FastAPI producer context drift.
- Fixed `/api/warmup/{instance_id}/status` so a Celery task that completes with a failed payload is reported as failed instead of succeeded.
- Added `progress_pct`, `started_at`, `elapsed_seconds`, and `estimated_remaining_seconds` to Kubernetes warmup Job aggregation for live ETA display.
- Added `active_phase`, `active_phase_label`, `active_message`, `active_last_log`, `phase_counts`, and `pod_statuses` to Kubernetes warmup status by combining Job state, pod state, and recent warmup pod logs.
- Added the idempotent `elb-scripts` ConfigMap builder/ensure path for the AKS warmup shell scripts.
- Added warmup-time AKS kubelet RBAC ensures for AcrPull and Storage Blob Data Reader using SDK model payloads and deterministic role assignment ids.
- Hardened node-local warmup scripts to remove `.azDownload-*` partial directories with recursive cleanup and to report actionable azcopy/download/vmtouch phases.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_auto_warmup.py api/tests/test_warmup_route.py api/tests/test_warmup_jobs.py` passed: 20 tests, including concurrent local Auto warm preference saves.
- `uv run ruff check api/services/auto_warmup.py api/tasks/storage.py api/tasks/azure.py api/routes/stubs.py api/tests/test_auto_warmup.py api/tests/test_warmup_route.py --select F821,F401,RUF012` passed.
- `npm run build` in `web/` passed.
- `npm run build` in `web/` passed after the compact cluster readiness-panel update.
- Local smoke after restarting API/worker/beat: `GET /api/health` returned 200, `PUT /api/warmup/auto-preference` returned 200 with `status=saved`, repo-root `.logs/local/state/auto_warmup.json` was created, and `http://127.0.0.1:8090/` returned 200.
- Runtime reconciler smoke with the saved local preference returned `status=completed` and `cluster_name=elb-cluster` with `status=not_ready` while AKS was stopped, so it did not enqueue warmup prematurely.
- Session browser reload at `http://127.0.0.1:8090/` rendered `ElasticBLAST Dashboard` and `Azure Kubernetes Service Cluster` with no error boundary.
- Session browser check while AKS reported `Starting` showed the compact cluster card using the readiness panel, with no `Submit pipeline · 15m` bento and no `Down` health pill in the cluster section.
- Browser check at `http://127.0.0.1:8090/` showed the DB Warmup panel with warm cache capacity (`10 nodes · Standard_E16s_v5`, memory headroom) and per-DB Warm actions for downloaded databases.
- Browser check showed the compact AKS database strip no longer renders sharded/downloaded-only DBs and instead shows `No warmed databases yet...` when no node warm cache exists.
- Browser check showed `core_nt` checked by default in the BLAST Databases modal Auto warm control, while non-downloaded DBs have Auto warm disabled.
- Live route check: `POST /api/warmup/start` with an intentionally unknown debug DB published to Redis `storage`, the fresh worker consumed it, and `GET /api/warmup/{task}/status` returned `output.status = failed` with `unknown database`.
- `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_warmup_jobs.py api/tests/test_warmup_route.py` passed: 25 tests for AcrPull/Storage role payloads, warmup phase inference, stale pod filtering, and warmup route status handling.
- Live diagnosis: `core_nt` warmup initially stopped before RAM warming because pods first lacked `elb-scripts`, then lacked ACR pull permission, then Storage denied shard manifests/data. The live cluster was remediated by creating/updating `elb-scripts`, assigning AcrPull and Storage Blob Data Reader to the AKS kubelet identity, and allowing the AKS managed subnet through the Storage account firewall with a Microsoft.Storage service endpoint.
- Live validation: `GET /api/monitor/aks/warmup-status` reported `core_nt` as `Loading`, `active_phase=copying_files`, `nodes_ready=9`, `nodes_active=1`, `progress_pct=90`, `active_message=Downloading shard files with azcopy`, and an estimated remaining time while the final shard copied.
- Browser validation at `http://127.0.0.1:8090/`: the compact AKS database strip rendered `core_nt · copying · 9/10 · ~55s left` with tooltip detail showing `Copying files to node disk`, `Downloading shard files with azcopy`, `done 9`, and `copying 1`.
- Node-side validation: inside the active shard-06 warmup pod, `azcopy` was running, the node-local shard directory was growing, and `/proc/meminfo` showed file-cache activity (`Cached` about 46 GiB, `Dirty` about 18 GiB), explaining why `kubectl top nodes` can still show low process memory even while warmup is populating Linux page cache.