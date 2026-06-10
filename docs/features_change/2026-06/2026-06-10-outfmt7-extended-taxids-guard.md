# Extended outfmt 7 (taxids) — boundary guard, OpenAPI example, routing docs

## Motivation

Follow-up hardening for extended tabular output like
`-outfmt "7 std staxids sstrand qseq sseq"` (taxonomy/strand/sequence columns).
Investigation found that *how* the specifier enters the pipeline silently
decides whether it works, because the two entry paths apply different
forbidden-character rules:

| Entry path | Forbidden chars | Quotes | Multi-token result |
|---|---|---|---|
| `outfmt` field ([config.py](../../api/services/blast/config.py)) | quotes + shell metas — **quotes banned** | ✗ | `-outfmt 7 std staxids` UNQUOTED → elastic-blast `shlex.split` hands `std`, `staxids` to BLAST as stray args → **silent cluster failure ~60 s later** |
| `additional_options` ([config.py](../../api/services/blast/config.py)) | shell metas only — **quotes allowed** | ✓ | `-outfmt "7 std staxids"` → `shlex.split` keeps it a single token ✓ |

All options are space-joined into the ini `blast.options`. So a multi-token
value in the bare `outfmt` field passed the boundary and broke only in-cluster —
a trap with no early signal.

## User-facing change (A, B, C)

- **A — boundary guard**: `generate_config` now rejects a multi-token value in
  the `outfmt` field with an actionable 422 ("outfmt only accepts a single
  format code here … pass it via additional_options as `-outfmt "7 std …"`").
  This converts the silent ~60 s cluster failure into an immediate, explained
  rejection at submit time.
- **B — OpenAPI example**: new `/v1/jobs` curated example
  `mode_b_core_nt_outfmt7_taxids` showing the extended layout with `std` first
  (`outfmt: "7 std staxids sstrand qseq sseq"`). Its description spells out the
  std-first requirement, the precise-mode path for Web BLAST equivalence, and an
  explicit CAVEAT that the env-var → shell quoting through to each shard pod is
  not yet end-to-end verified on a live sharded run.
- **C — routing docs**: this note documents the two-path table above and the
  recommended recipe.

## Recommended recipe (taxids under sharding, Web BLAST-equivalent)

1. Submit from New Search with `sharding_mode=precise` (search-space correction
   + tie-order oracle) for Web BLAST-equivalent e-values and ranking.
2. Provide the extended layout with `std` FIRST so the merge re-ranks by the
   fixed std positions and preserves the trailing columns:
   `-outfmt "7 std staxids sstrand qseq sseq"`.
3. Route it through the quoted path (`additional_options`, or the sibling
   `blast_options.outfmt` string which keeps it verbatim — do NOT also place
   `-outfmt` in `extra`, that double-specifies).
4. Validate on a non-production cluster first (the env→shell quoting caveat).

## API / IaC diff summary

- `api/services/blast/config.py`: reject a space-containing (multi-token)
  `outfmt` field value before it reaches the ini.
- `api/tests/test_blast_config_sharding.py`: guard rejects multi-token field;
  extended layout accepted via quoted `additional_options`; repointed the
  pre-existing `6 std qlen` test (which asserted the now-blocked unquoted field
  path) to the sanctioned quoted path.
- `web/src/pages/apiReference/spec.ts` + `spec.test.ts`: new
  `mode_b_core_nt_outfmt7_taxids` example with std-first specifier + caveats.

No Bicep / Container App changes.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_config_sharding.py` — passed
  (multi-token field rejected; quoted additional_options accepted).
- `uv run pytest -q api/tests -k "config or sharding or outfmt or merge or
  precision or web_blast or parity"` — 241 passed, 3 skipped.
- `cd web && npm test -- --run spec.test` — 5 passed; `npm run build` — passed.
- `uv run ruff check api/services/blast/config.py
  api/tests/test_blast_config_sharding.py` — passed.

## Deploy note

The guard (A) is live on the next api image; it changes only the error surface
for an input that previously failed in-cluster. The taxid example (B) is
documentation-only. The env→shell quoting for a live multi-token sharded run
remains the one un-verified link — exercise it on a non-production cluster
before relying on extended columns under sharding.
