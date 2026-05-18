# BLAST XML Results Preview

## Motivation

Completed BLAST jobs that use `-outfmt 5` produce XML result files, but the
dashboard analytics/export path only parsed tabular `-outfmt 6` / `-outfmt 7`
rows. Researchers could download the XML, but could not quickly inspect hits in
the browser or export one consolidated CSV in the same way they would review an
NCBI Web BLAST results table.

## User-facing change

- The results analytics page now opens on a **Hits** table that shows query,
  accession, organism/taxid, description, HSP query coverage, identity, length,
  e-value, bit score, source file, and a conservative review badge.
- The Hits / Alignments tabs now read all parseable result blobs by default
  instead of treating the first result file as the whole job. The UI reports
  returned, filtered, total-hit, and file-coverage counts and supports paging,
  sorting, accession text filtering, organism/taxid filtering, identity,
  HSP query-coverage, and e-value thresholds.
- `-outfmt 5` BLAST XML results are parsed into the same canonical hit model as
  tabular output, so overview stats, alignment cards, CSV, TSV, and JSON export
  work for XML-backed jobs.
- Result exports include computed query/subject coverage, source blob, and
  diagnostic review fields when those values are available.
- HSP coverage is computed from query/subject coordinate spans when coordinates
  are available, preventing gapped alignments from inflating review badges via
  raw alignment length.
- Alignment preview parsing stops at a server-side hit safety cap and surfaces
  the response as partial instead of letting very large result sets exhaust the
  API sidecar.
- Numeric hit values are rendered defensively when a malformed tabular result
  field has to be preserved as text, preventing one bad row from breaking the
  preview.
- Partially readable result sets are surfaced as degraded in the UI instead of
  being mistaken for clean no-hit jobs.
- Gzipped result blobs such as `merged_results.out.gz` are read through a bounded
  decompression path before parsing.

## API / IaC diff summary

- `api/services/blast_results_parser.py` adds `parse_blast_xml` and
  `parse_blast_result_content` for XML/tabular auto-detection, with
  namespace-tolerant XML element traversal.
- `api/services/storage_data.py` adds `read_result_blob_text`, which inflates
  `.gz` result blobs with a decompressed byte cap.
- `api/routes/stubs.py` expands the result parser target set from `.out` only
  to `.out`, `.out.gz`, `.xml`, and `.xml.gz`, and makes the alignment endpoint
  page/sort/filter across all parseable result blobs by default.
- `web/src/pages/BlastAnalytics.tsx` adds the Web BLAST-style Hits table and
  makes it the default analytics tab, with review badges and richer table
  controls for molecular diagnostic review.

No IaC changes. No new dependencies.

## Validation evidence

```
uv run pytest -q api/tests/test_blast_results_parser.py api/tests/test_blast_results_routes.py api/tests/test_storage_data.py
  52 passed in 1.36s

uv run pytest -q api/tests
  622 passed in 31.10s

uv run ruff check api
  All checks passed!

cd web && npx eslint src/pages/BlastAnalytics.tsx src/api/blast.ts --max-warnings 0
  passed

cd web && npm run build
  ✓ built in 5.06s
```

The full frontend build emitted the existing Vite chunk-size warning for the
main bundle; it did not fail the build.