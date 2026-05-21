---
title: Resource Plane (Agent Detail)
description: Celery task surface that mirrors azure-prereq.md — AKS provisioning, ACR build, BLAST submit / status / delete, database warmup, and scheduled reconciliation tasks.
---

# ElasticBLAST Resource Plane (detail)

> Extracted from `.github/copilot-instructions.md` §7 on 2026-05-19.

The web app is the source of truth for the *infrastructure* the elastic-blast CLI talks to. Implement these as **Celery tasks** in `api/tasks/` (queued onto the in-revision Redis broker, executed by the `worker` sidecar). The `api` sidecar enqueues the task and returns a task id; the SPA polls `/api/tasks/<id>` for progress. State (status, history, audit) lives in Azure Table Storage via `api/services/state_repo.py`.

| Celery task              | Mirrors azure-prereq.md | Notes                                                                                                                       |
| ------------------------ | ----------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `ensure_resource_groups` | Step 3                  | Two RGs: `rg-elb` (workload) and `rg-elbacr` (registry). Both names + region configurable in the UI.                        |
| `ensure_acr`             | Step 4                  | Standard SKU. Idempotent. Output: login server.                                                                              |
| `build_acr_images`       | Step 6                  | Use **`az acr build` REST API** (no local Docker). Build `ncbi/elb:1.4.0`, `ncbi/elasticblast-job-submit:4.1.0`, `ncbi/elasticblast-query-split:0.1.4`. Report per-image status. |
| `ensure_storage`         | Step 7                  | HNS-enabled `Standard_LRS`. Containers `blast-db`, `queries`, `results`. **Reachable from the Container App via private endpoint only** — see charter §9. |
| `monitor_aks`            | Step 9                  | Polls `aks list/show`, surfaces `provisioningState`, node count, `powerState`, kubelet identity, role assignments. Scheduled by `beat`.          |
| `monitor_jobs`           | Step 9.3                | Polls `kubectl get jobs/pods` via the kubelet API or via the `terminal` sidecar's loopback shell. Persists history to Table Storage. Scheduled by `beat`. |

Tasks must be **idempotent** (Celery retries on transient failures), **side-effect-tagged** in the docstring, and write progress checkpoints to the state repo so the UI shows real progress instead of a spinner.

Image tags MUST stay in sync with `src/elastic_blast/constants.py` in the sibling repo. Regression check: every task that builds images reads tag values from a single `IMAGE_TAGS` dict (`api/services/image_tags.py`) that future contributors can update in one place. Hard-code today's pinned tags; re-validate when bumping.
