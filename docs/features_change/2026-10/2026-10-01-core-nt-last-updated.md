---
title: Refresh core_nt OpenAPI last_updated
summary: The OpenAPI database detail response now reports the active core_nt generation's release timestamp instead of stale legacy metadata.
description: The OpenAPI database detail response now reports the active core_nt generation's release timestamp instead of stale legacy metadata.
tags:
  - blast
  - architecture
---

# Refresh core_nt OpenAPI last_updated

## Motivation

After an NCBI Direct `core_nt` generation was promoted in [Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-introduction), `GET /api/aks/openapi/databases/core_nt` returned the new generation ID and counts but retained `last_updated` from the stable legacy NCBI metadata blob. Consumers therefore saw a new snapshot paired with an older release timestamp.

## User-Facing Change

The database detail API now reports `last_updated` from the active generation's `source_release_at`. Older metadata records that store the release timestamp only at the top level remain supported. Databases without an active generation keep the existing metadata-blob value.

## API / IaC Diff Summary

- Updated the existing `last_updated` value in the OpenAPI-compatible database detail response; no response field was added or removed.
- Kept the active generation as the authoritative source and retained a top-level compatibility fallback.
- No IaC, authentication, RBAC, Storage network, or persistence schema changed.

## Validation Evidence

- `uv run pytest -q api/tests/test_aks_openapi_databases.py` passed: 32 tests.
- Full backend suite passed: 6,016 tests with 4 fixture-dependent skips.
- Ruff, the mypy debt ratchet, the 242-operation OpenAPI contract, generated TypeScript API types, and strict MkDocs build passed.
- Local API smoke passed: 27 of 27 checks.
- Regression coverage verifies both the active-generation timestamp and the legacy top-level fallback.
