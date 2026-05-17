# Web BLAST equivalence contract

## Motivation

The control plane is intended to replace NCBI Web BLAST for supported Azure workloads while running much faster on warmed AKS node-local shards. That claim needs a stricter default than approximate sharding: every Web-compatible run must use prepared shards plus full-database search-space correction, and comparator evidence must separate true mismatches from top-N tie-boundary diagnostics.

## User-facing change

- Warmed, prepared databases now prefer the `Web-equivalent shard` mode instead of `Fast shard` on the BLAST submit page.
- `Fast shard` remains available as an explicit throughput probe, but its copy no longer implies full Web BLAST equivalence.
- The Web CSV comparison helper now reports `tie_window_equivalent` and can exit successfully with `--accept-tie-window` when strict order fails only inside a shared top-N score class. Strict equality remains the final pass criterion for Web-equivalence claims.

## API/IaC diff summary

- No IaC changes.
- Frontend sharding availability now defaults eligible warmed DBs to `precise` and labels that mode as `Web-equivalent shard`.
- `scripts/dev/compare-blast-web-csv.py` now mirrors the Web XML/outfmt6 comparator's tie-window diagnostic shape for CSV evidence.
- `terminal/merge-sharded-results.sh` now records `tie_cutoff_overflow_count` and `tie_cutoff_queries` when `max_target_seqs` truncates a tied score class.
- `terminal/merge-sharded-results.sh` now accepts `ELB_TIE_ORDER_FILE`; when a same-snapshot accession order oracle is supplied, tied score classes are ordered by oracle rank before fallback ordinal. `ELB_TIE_ORDER_STRICT=1` also excludes non-oracle hits before truncation when the oracle defines top-N membership.
- `terminal/patch_elastic_blast.py` now patches the finalizer to download `${ELB_RESULTS}/${ELB_METADATA_DIR}/tie-order-oracle.txt`, export it as `ELB_TIE_ORDER_FILE`, and enable strict oracle mode before merging.
- `/api/blast/submit` options now accept `tie_order_oracle_text` or `tie_order_oracle_accessions`; the worker uploads that data to `results/<job>/metadata/tie-order-oracle.txt` for the finalizer.
- `scripts/dev/infer-blast-tie-order.py` records offline tie-order inference attempts and scores synthetic order keys against Web evidence.
- `docs/blast-searchsp-discovery.md` now includes the runtime equivalence contract: warmed prepared shards, `sharding_mode=precise`, verified full-DB `-searchsp`, merge-supported output, and comparator evidence.

## Validation evidence

- `uv run pytest -q api/tests/test_compare_blast_web_csv.py api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py` → 109 passed.
- `uv run pytest -q api/tests/test_blast_tasks.py::test_upload_tie_order_oracle_writes_finalizer_metadata api/tests/test_blast_tasks.py::test_upload_tie_order_oracle_rejects_oversized_payload api/tests/test_sharded_merge.py api/tests/test_compare_blast_web_csv.py` → 10 passed.
- `uv run pytest -q api/tests` → 593 passed.
- `cd web && npm run test -- shardingAvailability` → 4 passed.
- `cd web && npm run build` → passed; Vite emitted the existing large chunk warning.
- Runtime observation: local compose sidecars are healthy; dashboard/API show `elb-cluster` Running in `rg-elb-01`; `core_nt` warmup is Ready on 10/10 shards; worker `reconcile_auto_warmup` returns `already_ready`.
- `/api/blast/pre-flight` with an existing precise sharded `core_nt` payload returns `ready: true`, `critical_blockers: 0`, and `sharding_precision.precision_level: precise_single_query`.
- Comparator rerun: no-hit `core_nt` calibration remains strictly equivalent (`canonical-compare.json` reports `equivalent: true`, `difference_count: 0`). Current F3L positive-hit Web XML/CSV evidence remains non-equivalent to the final sharded top-500 candidate. A wide-pool XML comparison reports `shared_accessions: 500`, `web_only: 0`, `value_mismatch_count: 0`, and `tie_window_equivalent: true`, confirming the next work item is top-N tie/order merge optimization rather than candidate generation.
- Merge diagnostic rerun on the wide F3L candidate pool reports `total_input_hits: 11261`, `tie_break_count: 11085`, and `tie_cutoff_overflow_count: 8620`; the cutoff score class has 9,120 tied hits and only 500 can be emitted.
- Tie-order inference evaluated 249 synthetic keys; the best key still reached only `top500_overlap: 33`, so local metadata does not justify a fabricated production tie-breaker.
- Oracle proof: using the Web top-500 accession list as `ELB_TIE_ORDER_FILE` with strict mode against the same wide pool yields strict comparator success: `equivalent: true`, `exact_order: true`, `shared_accessions: 500`, `web_only: 0`, `candidate_only: 0`, `value_mismatch_count: 0`, and `tie_cutoff_overflow_count: 0`.
- 16S same-snapshot proof: using the local full-run XML accession order as a strict oracle against contiguous sharded XML artifacts yields exact 500/500 accession order; remaining canonical XML differences are `Hit_id` GI-prefix and five `Hit_def` provenance differences from synthetic FASTA shard DB regeneration.
- `uv run ruff check api/tasks/blast.py api/routes/stubs.py api/tests/test_blast_tasks.py terminal/patch_elastic_blast.py scripts/dev/infer-blast-tie-order.py scripts/dev/compare-blast-web-csv.py api/tests/test_compare_blast_web_csv.py api/tests/test_sharded_merge.py` → passed.
