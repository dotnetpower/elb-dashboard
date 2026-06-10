# Quote-safe multi-token outfmt to shard pods (UNQUOTED wire format)

## Motivation

To surface subject taxids/names, the BLAST search needs a multi-token tabular
outfmt, e.g. `-outfmt 7 sseqid staxids sstrand pident evalue bitscore ...`. Two
hot-path mechanisms broke a multi-token specifier on the way to each shard pod:

1. **YAML injection** — elastic-blast injects `ELB_BLAST_OPTIONS` into the job
   YAML via `substitute_params`, a raw `${VAR}` regex replace with NO YAML
   escaping (`value: "${ELB_BLAST_OPTIONS}"`). A quoted value
   (`-outfmt "7 ..."`) closes the YAML scalar early → broken manifest.
2. **Shell word-splitting** — `blast-run-aks.sh` expands `$ELB_BLAST_OPTIONS`
   UNQUOTED (SC2086 disabled on purpose), so `-outfmt "7 ..."` (or an unquoted
   `-outfmt 7 std staxids`) splits into stray positional args for `blastn`.

The fix is a single canonical wire format that needs no quotes at all:
**UNQUOTED multi-token outfmt** in the ini/env, with each consumer rejoining the
`-outfmt` tokens. Quotes are never introduced, so the YAML injection stays valid
and there is no `eval`/escaping anywhere.

> Correction to the prior change note
> `2026-06-10-outfmt7-extended-taxids-guard.md`: that note recommended passing
> the specifier QUOTED via `additional_options`. That is wrong for the YAML path
> (raw substitution breaks on the quote). The canonical format is UNQUOTED; the
> rejoin in the merge + run script recovers the full specifier.

## Scope (gates still CLOSED — not reachable from a production submit)

The FE / backend / elastic-blast merge gates still admit only
`5/6/6 std…/7/7 std…`, so a reordered/extended specifier cannot reach these
paths from a production submit. The run-script patch is byte-identical for the
single-token `-outfmt` every current job uses, so rebuilding the terminal /
OpenAPI image is safe for existing runs; the multi-token path is exercised only
by isolated tests until the gates open AND a non-prod live run confirms the
end-to-end YAML→env→shell→blastn flow.

## API / IaC diff summary

- `terminal/merge-sharded-results.sh` `parse_outfmt_spec()`: rejoin every token
  after `-outfmt` up to the next `-flag`, so the full specifier is recovered
  from the UNQUOTED wire format (previously it took only the single token right
  after `-outfmt`, which silently dropped the extended fields).
- `terminal/patch_elastic_blast.py` new `patch_blast_run_aks_outfmt_argv`
  (wired into `patch_blast_run_aks_script`): rebuilds `ELB_BLAST_OPTIONS` into a
  quote-safe `ELB_BLAST_ARGV` array, rejoining a multi-token `-outfmt` into one
  element, and swaps the `blastn` invocation to `"${ELB_BLAST_ARGV[@]}"`. No
  `eval`, no quotes. Byte-identical for single-token `-outfmt`. Skips gracefully
  when the anchor is absent; raises if the anchor is present but the invocation
  line drifted. No YAML template change is needed (unquoted ⇒ valid YAML).

## Validation evidence

- `api/tests/test_terminal_patch_elastic_blast.py` (runs the patched stub in
  bash): single-token `-outfmt 5` → argv byte-identical to plain
  word-splitting; multi-token `-outfmt 7 sseqid staxids …` → ONE grouped argv
  element; `-outfmt` at end grouped; patch idempotent.
- `api/tests/test_sharded_merge.py::…_unquoted_multitoken` — the merge resolves
  the full specifier from the UNQUOTED form.
- Full `patch_elastic_blast.py` applied to a copy of the REAL sibling tree:
  no crash, the argv rebuild + `"${ELB_BLAST_ARGV[@]}"` land exactly once.
- `uv run pytest -q api/tests -m '' -k "shard or merge or sharding or precision
  or terminal_patch or web_blast or parity"` — 257 passed, 3 skipped.
- `uv run ruff check …` — passed; embedded merge python `ast.parse` clean.

## Remaining for end-to-end (gates still closed)

1. Dashboard: normalise the submitted outfmt to lead with `qseqid` (the merge +
   read-side aggregation key on it) and emit the multi-token specifier UNQUOTED.
2. Open the FE/backend/elastic-blast gates to `7 <arbitrary fields>`.
3. Live-verify on a NON-PRODUCTION cluster (the YAML→env→shell→blastn quoting is
   the one link that cannot be unit-tested) before any production image rebuild.
