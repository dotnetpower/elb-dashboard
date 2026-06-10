# outfmt 7 sharded BLAST support (New Search + OpenAPI)

## Motivation

New Search disabled all sharded performance modes whenever the output format was
set to 7 ("Tabular + comments"), and the OpenAPI `/v1/jobs` plane could not run
outfmt 7 sharded either. Researchers who want tabular output with the BLASTN /
Query / Fields / hit-count comment headers were forced onto the slower full-DB
path.

outfmt 7 is the same 12-column tabular layout as outfmt 6 with added comment
lines. The shard merge already skips `#` comment lines on input and re-emits its
own comment headers, so outfmt 7 merges correctly through the existing tabular
path. Because a plain `-outfmt 7` is a single token (unlike a multi-token
`-outfmt "6 std staxids"`), it carries no YAML/shell quoting hazard and is safe
to thread end-to-end.

## User-facing change

- New Search: selecting output format 7 no longer disables the Fast shard /
  Precise shard modes — sharding works for outfmt 7 exactly as for 5 and 6.
- OpenAPI: a new curated `/v1/jobs` example `mode_b_core_nt_outfmt7` requests
  `blast_options.outfmt: "7"` so callers can copy a working outfmt 7 sharded
  submit.

This does NOT add taxonomy columns (`staxids`). Plain outfmt 7 contains no taxid
column; that requires a multi-token field specifier and is tracked separately.

## API / IaC diff summary

- `api/services/sharding_precision.py` `merge_format_for_outfmt`: accept `7` and
  `7 std` as the tabular merge family; both blocker messages mention outfmt 7.
- `api/services/blast/config.py`: sharded-merge error message mentions outfmt 7.
- `terminal/merge-sharded-results.sh`: dispatch `outfmt in ("6", "7")` to the
  tabular merge and record the real outfmt in the report (was hardcoded 6).
- `terminal/patch_elastic_blast.py`: new `patch_partitioned_outfmt_gate` widens
  the vendored elastic-blast `elb_config.py` partitioned-outfmt gate to allow
  `7` / `7 std`. Registered in `main()`. The patch is also copied into the
  OpenAPI build context, so `/v1/jobs` execution gets the same gate.
- `web/src/pages/blastSubmit/shardingAvailability.ts` `isMergeCompatibleOutfmt`:
  accept 7; the unavailable reason now reads "5, 6, or 7".
- `web/src/pages/apiReference/spec.ts`: new `mode_b_core_nt_outfmt7` example.

No Bicep / Container App changes. `ExternalBlastSubmitRequest.outfmt` stays
`Literal[5]` (its XML→FASTA downstream is unchanged); the outfmt 7 example uses
the sibling's free-form `blast_options.outfmt`, which is the real OpenAPI path.

## Validation evidence

- `terminal/patch_elastic_blast.py` gate applied to a copy of the REAL sibling
  `elb_config.py` → matched byte-for-byte and widened cleanly (drift guard).
- `api/tests/test_sharded_merge.py::test_merge_sharded_results_supports_outfmt7_tabular`
  runs the actual `merge-sharded-results.sh` with `-outfmt 7`: comment lines are
  skipped, data merges + re-ranks, report records outfmt 7.
- `api/tests/test_blast_config_sharding.py` (outfmt 7 accepted / 11 rejected),
  `api/tests/test_sharding_precision.py` (7 eligible / 11 blocked),
  `api/tests/test_terminal_patch_elastic_blast.py` (gate widened + idempotent).
- `uv run pytest -q api/tests` — 3217 passed, 3 skipped.
- `cd web && npm test -- --run` — 769 passed (shardingAvailability + spec).
- `cd web && npm run build` — type-check + build passed.
- `uv run ruff check api terminal/patch_elastic_blast.py` — passed.

## Deploy note

Takes effect live only after the terminal image is rebuilt (for New Search
sharding) and `elb-openapi` is redeployed (for `/v1/jobs`), because the
elastic-blast gate is patched at image build time. Several pre-existing tests
that used outfmt 7 as the "unsupported" example were repointed to outfmt 11.
