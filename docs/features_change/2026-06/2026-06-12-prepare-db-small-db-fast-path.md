---
title: "prepare-db: small-DB server-side routing, adaptive poll, pre-baked azcopy image"
description: "Three speed/reliability improvements to the AKS-fanout prepare-db path so small DB updates are near-instant and large downloads no longer pay a per-pod azcopy GitHub download."
tags:
  - blast
  - infra
---

# prepare-db: small-DB server-side routing + adaptive poll + pre-baked azcopy image

## Motivation

A tiny DB update (e.g. `16S_ribosomal_RNA`, ~18 MB / 15 files) felt slow and
"stuck". App Insights confirmed the update itself succeeded
(`prepare_db_via_aks done outcome=promoted`), but each run took 60-210 s. The
time was **fixed AKS-fanout bootstrap overhead** — pod scheduling, the
`mcr.microsoft.com/azure-cli` image pull, the per-pod azcopy download from
GitHub, `azcopy login --identity`, and the 30 s Celery poll granularity — all
of which dwarf the actual transfer for a small DB. The AKS-fanout path is
designed for hundreds-of-GB databases (`nt` / `core_nt`); it is the wrong tool
for a small one.

## User-facing change

* **Small DB updates are near-instant.** In `mode=auto`, a DB whose total
  NCBI size is under the threshold (default 1 GiB; or ≤ 30 files when sizes are
  unknown) now uses the server-side `start_copy_from_url` path — a
  server-to-server async copy that needs no cluster and no pod bootstrap.
  Large DBs are unchanged and still fan out across AKS. Explicit `mode=aks`
  always honours the caller and skips this shortcut.
* **Faster completion detection on the AKS path.** The Job poll loop now starts
  at 5 s and doubles up to a 30 s ceiling instead of a fixed 30 s tick, so a
  medium AKS job that finishes quickly is detected in ~5-35 s; multi-hour
  downloads settle at 30 s so the K8s API is not hammered.
* **Large downloads no longer depend on a per-pod GitHub azcopy download.** A
  new pre-baked `elb-prepare-db` image (Azure CLI + pinned azcopy 10.28.0) is
  built into the workload ACR and used as the Job image. The pod entrypoint
  auto-detects the pre-installed binary and skips the download; if the image is
  ever absent the resolver falls back to the public image + GitHub download
  (unchanged legacy behaviour).

## API / IaC diff summary

Backend (api/worker image — ships via `quick-deploy.sh api worker`):

- `api/services/storage/prepare_db_aks_params.py`
  - new pure `prefer_server_side_for_small_db(total_bytes, file_count)` gated by
    `PREPARE_DB_AKS_MIN_TOTAL_BYTES` (default 1 GiB) and
    `PREPARE_DB_AKS_MIN_FILE_COUNT` (default 30, used only when sizes unknown).
  - `resolve_aks_job_limits().image` now treats an empty
    `PREPARE_DB_AKS_AZCOPY_IMAGE` as the public default (safe fallback).
- `api/routes/storage/prepare_db.py` — `_try_dispatch_aks_mode` consults the
  helper for `mode=auto` and returns `None` (server-side fall-through) BEFORE
  taking the lock / writing start metadata, so no state is left behind.
- `api/tasks/storage/prepare_db_via_aks.py` — adaptive poll cadence
  (`PREPARE_DB_AKS_JOB_POLL_INITIAL_SECONDS` default 5, doubling to
  `PREPARE_DB_AKS_JOB_POLL_INTERVAL_SECONDS` default 30).

Infra (full `azd provision` / `postprovision.sh` — needs the image build + env):

- `aks/prepare-db/Dockerfile` — new pre-baked image (Azure CLI 2.81.0 + azcopy
  10.28.0, extracted with stdlib tarfile; no Dockerfile heredoc so ACR Tasks
  build it without BuildKit).
- `scripts/dev/postprovision.sh` — builds `elb-prepare-db` alongside
  api/frontend and verifies it in the final pass.
- `infra/modules/containerAppControl.bicep` — api sidecar gains
  `PREPARE_DB_AKS_AZCOPY_IMAGE = <acr>/elb-prepare-db:<tag>` (empty when there
  is no ACR → resolver falls back to the public image). The compiled
  `infra/*.json` artifacts are not regenerated here — they were already stale
  in the repo and the deploy path compiles the `.bicep` directly.

No RBAC change: the AKS kubelet already holds `AcrPull` on the workload ACR
(the same grant elastic-blast job images rely on), so the pre-baked image is
pullable without any new role assignment.

## Validation

- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests` — 3324 passed, 3 skipped.
- New / updated tests:
  - `api/tests/test_prepare_db_aks_params.py` — size-routing helper (known
    small → server-side, large → AKS, unknown-size file-count gate, env
    overrides).
  - `api/tests/test_prepare_db_aks_route.py` — `mode=auto` + small DB falls
    through to server-side and does NOT dispatch the AKS task, even when AKS is
    healthy; the existing availability-routing test disables the size gate.
- Dockerfile azcopy-extraction one-liner validated locally against a fake
  tarball (`EXTRACT_OK` + `azcopy` runs).

> #2 (pre-baked image) and the api `PREPARE_DB_AKS_AZCOPY_IMAGE` env take effect
> only after a full `azd provision` / `postprovision.sh` run (image build +
> container-app env). #1 and #3 ship with a normal api/worker image deploy.
