# API Reference: small 16S ribosomal RNA example for POST /v1/jobs

## Motivation

The API Reference page's **POST /v1/jobs** card shipped a single curated
request body example (`Mode B - Web BLAST-equivalent core_nt`). That makes
the Try it surface unusable until the `core_nt` database (~700 MB) is
staged in workload Storage and warmed on AKS. On a brand-new deployment
the small `16S_ribosomal_RNA` database (~50 MB) is the first thing the
Get-started guide tells researchers to download, so it should also be the
default Try it example.

## User-facing change

The **Request Body** dropdown of `POST /v1/jobs` in the API Reference page
now lists:

1. `Small - 16S ribosomal RNA (~50 MB DB)` — default selection. Uses
   `db: "16S_ribosomal_RNA"`, the E. coli K-12 MG1655 16S rRNA partial
   query (`NR_024570.1`, ~490 bp), `evalue 0.01`, `max_target_seqs 50`,
   `outfmt 5`, and `resource_profile: "standard"`.
2. `Mode B - Web BLAST-equivalent core_nt` — unchanged.
3. Any other examples published by the upstream OpenAPI spec, in their
   original order.

Researchers can now exercise the `/v1/jobs` Try it flow as soon as the
small 16S database is Ready, without having to wait for `core_nt` to
stage.

## API / IaC diff

None. The change is a frontend static-data update only:

- [web/src/pages/apiReference/spec.ts](../../../web/src/pages/apiReference/spec.ts)
  — added `SMALL_16S_RRNA_FASTA` + `SMALL_16S_RRNA_JOB_EXAMPLE`,
  prepended `small_16s_rrna` in `withCuratedRequestExamples`, and made
  `getDefaultRequestExampleKey` prefer it for `POST /v1/jobs`.
- [web/src/pages/apiReference/spec.test.ts](../../../web/src/pages/apiReference/spec.test.ts)
  — updated the curated-examples and default-key tests to assert the
  new ordering and default selection.

No backend, OpenAPI, or Bicep change. The `db: "16S_ribosomal_RNA"`
value is already a first-class entry in
`api/tasks/storage/helpers.py::BLAST_DATABASES`, so the rest of the
submit pipeline (admission gates, OpenAPI proxy, results download)
needs no adjustment.

## Validation

- `cd web && npm test -- --run src/pages/apiReference/spec.test.ts`
  — 5/5 passing (new ordering + default-key assertions).
- `cd web && npm test -- --run` — 51 files / 383 tests passing.
- `cd web && npm run lint` — clean (0 warnings).
- `cd web && npm run build` — production build succeeds.
- Manual: opened `POST /v1/jobs` card in the API Reference page; the
  dropdown shows `Small - 16S ribosomal RNA (~50 MB DB)` selected by
  default, switching to `Mode B - Web BLAST-equivalent core_nt`
  replaces the body with the core_nt example unchanged.
