# NCBI-style result download options

## Motivation

Researchers expect the BLAST result page to offer the same download vocabulary as NCBI Web BLAST, especially XML, JSON Seq-align, and Hit Table exports, while preserving the dashboard default output format of BLAST XML (`outfmt 5`).

## User-facing change

The BLAST result header's Download all menu now shows NCBI-style entries. XML downloads stream the captured BLAST XML for default `outfmt 5` jobs, Hit Table text/CSV entries export parsed hits, and JSON Seq-align exports parsed HSP rows with sequence fields when the source format includes them. Raw-only formats such as ASN.1, XML2, BLAST JSON, and SAM are shown as submit-time capture formats rather than synthesized after the run.

The New BLAST search form still defaults to `outfmt 5`, and its Output format selector now exposes additional BLAST+ formats so future jobs can capture raw ASN.1, JSON, XML2, or SAM output when desired.

Local development startup now derives the Table/Blob endpoints from the active azd `STORAGE_ACCOUNT_NAME` when `ELB_LOCAL_STORAGE_ACCOUNT` is not set. This prevents the Recent searches page from degrading against the stale `elbstg01` default after a local API restart.

## API/IaC diff summary

- `/api/blast/jobs/{job_id}/results/export` accepts `hit-table-text`, `hit-table-csv`, `json-seqalign`, `xml`, and `text` in addition to the existing `csv`, `tsv`, and `json` values.
- `scripts/dev/local-run.sh` uses azd environment values for local Storage endpoints before falling back to the legacy demo defaults.
- No infrastructure changes.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_results_routes.py -k "export"`
- `npm run build` in `web/`
- `bash -n scripts/dev/local-run.sh`
- `curl http://127.0.0.1:8085/api/blast/jobs?...` returned job rows without `degraded` after API restart.