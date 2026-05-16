# outfmt 5 XML Sharded Merge Support

## Motivation

Researchers may need BLAST XML (`outfmt 5`) results while still using precise sharded ElasticBLAST. The previous merge contract only allowed tabular `outfmt 6`, so XML requests were blocked for sharded submissions.

## User-facing change

Sharded BLAST precision checks now treat `outfmt 5` as a supported XML merge format. Split-query parent finalization rewrites child XML results into one valid BLAST XML document instead of concatenating gzip members.

## API / task diff summary

- `api.services.sharding_precision` classifies `outfmt 5` as `xml_top_n` / `query_group_split_xml_top_n`.
- `api.services.blast_config` permits sharded `outfmt 5` while continuing to reject unsupported formats.
- `api.tasks.blast` aggregates XML child merge reports and assembles parent XML output via `BlastOutput_iterations` concatenation.
- `terminal/merge-sharded-results.sh` is synchronized with the sibling runtime XML-aware merge helper.
- `web/src/api/blast.ts` includes XML precision levels in the typed precision response.

## Supported precision levels

XML (`outfmt 5`) is supported for:

- `approximate` with `merge_strategy=xml_top_n`.
- `precise_single_query` with `merge_strategy=xml_top_n`.
- `precise_xml` for multi-query submissions with a uniform effective search space.
- `precise_xml_split` for mixed effective search spaces using query-group child jobs.

The merged XML is structurally valid and deterministic for the same child artifacts. Byte-identical XML parity with a single full-DB BLAST run is not claimed; semantic hit/HSP ordering is audited through `merge-report.json`.

## Hardening notes

- `-outfmt=5` and `-outfmt=7` syntax is parsed the same way as `-outfmt 5` / `-outfmt 7`.
- Dashboard split-parent XML assembly renumbers `Iteration_iter-num` sequentially.
- Mixed split-child merge formats or precision levels are rejected instead of being silently aggregated.
- The sibling finalizer treats missing shard outputs and unreadable shard results as fatal before writing success markers.
- Malformed shard XML is fatal in the merge helper, avoiding valid-but-incomplete merged XML.
- The sibling runtime rejects custom `outfmt 6` column layouts for partitioned merge, matching the dashboard policy of allowing only default `6` or `6 std...` layouts.

## Validation evidence

- `uv run pytest -q api/tests` -> 383 passed.
- `uv run pytest -q api/tests/test_sharded_merge.py api/tests/test_sharding_precision.py api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py` -> 112 passed.
- `cd web && npm run build` -> passed; Vite reported the existing large chunk warning.
- Sibling runtime validation: `PYTHONPATH=src python -m pytest -q tests/azure` -> 289 passed, 7 skipped.
- Sibling targeted validation: `PYTHONPATH=src python -m pytest -q tests/azure/test_db_partitioning.py` -> 38 passed.
- Synthetic XML semantic equivalence smoke: expected full-order `['subject_best', 'subject_bit']` matched merged sharded order `['subject_best', 'subject_bit']`.
