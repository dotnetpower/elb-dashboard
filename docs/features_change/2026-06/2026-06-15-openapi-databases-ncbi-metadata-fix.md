---
title: Database metadata endpoint reads NCBI metadata blobs for a true elb-openapi drop-in
description: Fix the cluster-independent GET /api/aks/openapi/databases/{db_name} so single-volume databases (16S/18S/ITS) return correct molecule_type, counts, and title instead of nulls, by reading the same NCBI metadata blobs elb-openapi reads.
tags:
  - blast
  - operate
---

# Database metadata endpoint reads NCBI metadata blobs for a true elb-openapi drop-in

## Motivation

The cluster-independent `GET /api/aks/openapi/databases/{db_name}` endpoint
(added earlier in [#37](https://github.com/dotnetpower/elb-dashboard/issues/37))
was advertised as a **drop-in for the `elb-openapi`
`GET /v1/databases/{db_name}` `DatabaseMetadata` response**. A self-critique pass
measured the live deployment against the in-cluster `elb-openapi` service and
found that claim was **false for the common single-volume databases**:

| Database | `elb-openapi` (authoritative) | dashboard endpoint (before) |
|----------|-------------------------------|------------------------------|
| `core_nt` | `molecule_type=dna`, `number_of_sequences=125940211`, `title="Core nucleotide BLAST database"` | `dna`, `125940211`, `title=""` |
| `16S_ribosomal_RNA` | `dna`, `27648`, `"16S ribosomal RNA…"` | **`molecule_type=null`, counts `null`, `title=""`** |
| `18S_fungal_sequences` / `ITS_RefSeq_Fungi` | `dna`, correct counts + title | all-null, same as 16S |

### Root cause

The endpoint sourced its detail from the dashboard's shared catalogue cache
(`storage.database_list.list_databases`), which enriches entries only from the
BLAST v5 `.njs` sidecar. That enrichment is incomplete: multi-volume databases
(`core_nt`) get counts but no title, and single-volume databases (16S/18S/ITS)
get **no** `molecule_type` / counts / title at all. `elb-openapi`, by contrast,
reads `blast-db/{db}/{db}-nucl-metadata.json` (or `-prot-metadata.json`) and
decides `molecule_type` by **which suffix blob exists** — which is exactly where
the authoritative molecule type, title, snapshot, and counts live.

An external caller parsing the response against the `elb-openapi`
`DatabaseMetadata` schema (`molecule_type: Literal["dna","protein"]` required)
would hit a validation error / mislabel for 16S and friends.

## User-facing change

`GET /api/aks/openapi/databases/{db_name}` now reads the **same NCBI metadata
blobs** as `elb-openapi`:

1. Try `blast-db/{db}/{db}-nucl-metadata.json` → `molecule_type="dna"`,
   `molecule_label="mixed DNA"`.
2. Else try `blast-db/{db}/{db}-prot-metadata.json` → `molecule_type="protein"`,
   `molecule_label="protein"`.
3. Both absent → HTTP 404 (genuine miss). A transient Storage failure on either
   candidate is re-raised → HTTP 503 (never mistaken for a miss).

Field mapping mirrors `elb-openapi`'s `normalise_metadata` byte-for-byte:
`title` = first non-empty of `description` / `display_name` / `title` / `name`;
`snapshot` = the `YYYY-MM-DD-HH-MM-SS` stamp extracted from the metadata
`files[]` paths (`"unknown"` if absent); `number_of_*` / `bytes_*` from the
hyphenated NCBI keys; `metadata_schema_version` from `version`.

`GET /api/aks/openapi/databases` (the list) is **unchanged** — the catalogue
cache returns the correct set of database names, and the heavy enumeration is
already warmed by the SPA.

## API / IaC diff summary

- `api/services/openapi/databases.py` — `get_database` now reads the NCBI
  `nucl`/`prot` metadata blobs directly (via `storage.data._blob_service` +
  `storage.blob_io.read_metadata_blob_bytes`) instead of projecting a catalogue
  cache entry. New helpers: `_snapshot_from_raw` (snapshot regex), `_title_from_raw`,
  `_raw_int`; `_resolve_molecule` now keyed by the suffix token (`nucl`/`prot`).
  `_project_metadata` signature changed to `(db_name, raw, molecule_token,
  container)`. `list_databases` unchanged.
- `api/routes/aks/openapi_databases.py` — docstring updated; route logic
  unchanged (None → 404, transient → 503 via `classify_storage_failure` still
  holds because `get_database` re-raises transient failures).
- `api/tests/test_aks_openapi_databases.py` — detail tests rewritten to mock the
  NCBI metadata blob reader; added the 16S regression (full fields), prot
  fall-through, both-absent → None, transient → raise, nucl-404-then-prot-transient
  → raise, snapshot-unknown, and `_raw_int` edge cases.
- No IaC change.

## Validation evidence

- Live measurement (before fix) confirming the gap: `elb-openapi` vs dashboard
  for `core_nt` / `16S_ribosomal_RNA` / `18S_fungal_sequences` / `ITS_RefSeq_Fungi`
  (table above), captured via `kubectl exec elb-openapi -- curl /v1/databases/<db>`
  and `curl /api/aks/openapi/databases/<db>`.
- `uv run pytest -q api/tests/test_aks_openapi_databases.py` — 17 passed.
- `uv run pytest -q api/tests` — 3730 passed, 3 skipped.
- `uv run ruff check` on the changed files — clean.
- Live re-confirmation requires a backend redeploy (the api sidecar image must
  carry the new resolver); to be verified in the browser / curl after the next
  `quick-deploy.sh api`.
