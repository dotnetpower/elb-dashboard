---
title: NCBI Direct database generations
description: Add an opt-in, generation-safe HTTPS path for NCBI database releases that have not reached the S3 cloud mirror.
tags: [blast, ui, architecture]
---

# NCBI Direct database generations

## Motivation

The [NCBI BLAST database S3 mirror](https://registry.opendata.aws/ncbi-blast-databases/) can lag the official [NCBI BLAST database distribution](https://ftp.ncbi.nlm.nih.gov/blast/db/v5/). On 2026-08-28, `core_nt` remained at an S3 content release of 2026-07-18 while the official distribution advertised 2026-08-19. Waiting for S3 preserves the fast cloud-to-cloud path but can leave research searches a month behind.

## User-facing change

- When enabled by the deployment operator, a pending database update offers an explicit **NCBI Direct (HTTPS)** action instead of waiting indefinitely for S3.
- The confirmation identifies the content release, archive count, transfer size, slower expected duration, and generation-safe promotion behavior.
- The database row distinguishes the NCBI content release, Azure activation time, and cloud snapshot or Direct generation identifier.
- The default remains disabled. No background or Auto warm process can start a Direct transfer without the operator-confirmed Update action.

## API, worker, and infrastructure summary

- `POST /api/storage/prepare-db` accepts `source=ncbi-direct` only with `mode=aks`; no stale-S3 fallback is allowed.
- NCBI metadata is normalized from the fixed official host to HTTPS. Every archive URL, official MD5, and Content-Length is pinned into a SHA-256 transfer manifest before dispatch.
- An [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) Indexed Job downloads one archive per index with bounded global parallelism, verifies MD5 before extraction, rejects non-regular/path-escaping archive members, applies an expansion bound, and uploads to a generation-specific [Azure Blob Storage](https://learn.microsoft.com/azure/storage/blobs/storage-blobs-introduction) prefix.
- The same immutable transfer manifest includes the matching `taxdb` archive, and a deployment-wide owner-checked Redis lock permits only one Direct transfer at a time. Pods are restricted to AKS User pools and request bounded ephemeral storage derived from the largest archive.
- Promotion requires every archive marker, matching transfer hash, exact staged file size, and complete generation-scoped shard layouts. Metadata promotion uses the existing ETag owner fence. Failure preserves the current active generation.
- BLAST requests resolve and persist the active generation URL when accepted, so a later promotion cannot change the database of a queued or retried request.
- `PREPARE_DB_NCBI_DIRECT_ENABLED=false` is wired in Bicep. Planned flip review: 2026-09-15 after a small-DB live transfer and cancellation/restart soak.
- No SAS URL, public Storage toggle, new Azure resource, or new dependency was added.

## Validation evidence

- `uv run ruff check api` passed.
- Full backend suite: `5484 passed, 4 skipped`; `uv run ruff check api` and strict mypy for the six new boundary modules passed.
- Real read-only NCBI smoke for `16S_ribosomal_RNA`: release `2026-08-25`, one 72,164,324-byte archive, official MD5 `42713f80e8268158178dd7c9d72105cc`, generation `ncbi-direct-20260825-4947f244d084`.
- Frontend: `109` test files and `984` tests passed; full ESLint and `npm run build` passed. Direct button gate coverage also verifies AKS-unavailable disable behavior.
- Local host-mode smoke: `27/27 passed` against the detached full stack.
- Bicep compile and parameter JSON validation passed. Live `azd provision --preview` was unavailable because the local Azure CLI refresh token had expired; no deployment was attempted.
- Full backend/frontend/docs and Bicep validation results are recorded before commit.