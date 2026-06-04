# 2026-06-04 — OpenAPI Mode B `/v1/jobs` 16S example fix (4.18 → 4.19)

## Motivation

The API Reference page renders the curated `POST /v1/jobs` request examples
served by the on-AKS `elb-openapi` pod (sibling
`docker-openapi/app/main.py`). Of the five examples, the two upstream Mode B
examples (`mode_b`, `mode_b_taxid`) were biologically nonsensical:

* `query_fasta` carried a real accession header (`>NC_003310.1`, Monkeypox
  virus) but a synthetic `ATGCATGC…` repeat instead of an actual sequence.
* That query was submitted against the bacterial `16S_ribosomal_RNA`
  database, which yields zero hits.
* `mode_b_taxid` additionally filtered with `taxid 10244` (Monkeypox) over a
  bacterial 16S DB — a meaningless combination.

All five examples passed schema validation (`JobSubmitRequest` does not use
`extra="forbid"`), so the breakage was a content/correctness defect, not a
parse error. A user copy-pasting the example as a starting point would get an
empty result set and a confusing taxid.

## User-facing change

The two upstream Mode B examples now use a real, coherent query:

* Shared `_SAMPLE_16S_FASTA` constant — E. coli K-12 MG1655 16S rRNA partial
  sequence (NCBI `NR_024570.1`, ~540 bp), matching the dashboard's own
  `small_16s_rrna` curated example.
* `mode_b` — real 16S query vs `16S_ribosomal_RNA`, now also emits
  `outfmt: "5"` (BLAST XML) which the result pipeline requires.
* `mode_b_taxid` — same real query, `taxid` corrected from `10244`
  (Monkeypox) to `562` (Escherichia coli), `outfmt: "5"` added.

`searchsp` was intentionally **not** added: per
`docs/research/blast-searchsp-discovery.md`, the effective search space is not
a fixed constant and the `core_nt`-specific `32156241807668` value must not be
reused for a different database.

## API / IaC diff summary

* Sibling `elastic-blast-azure` (separate repo, committed + pushed):
  * `feat(api): add E. coli K-12 16S rRNA sample for Mode B examples and update
    descriptions` (`1c4bb176`) — example content fix.
  * `chore(api): bump VERSION to 3.7.4 for Mode B 16S example fix`
    (`1af0e3f1`) — `VERSION 3.7.3 → 3.7.4`.
* Dashboard `api/services/image_tags.py` — `elb-openapi` pin `4.18 → 4.19`
  (4.19 == upstream 3.7.4), comment block updated with the mapping.
* No dashboard runtime behaviour change — examples are served by the
  `elb-openapi` pod; the dashboard only proxies and renders them.

## Validation evidence

* `_build_options` faithful reproduction renders:
  * `mode_b` → `-evalue 0.05 -max_target_seqs 100 -outfmt 5`
  * `mode_b_taxid` → `-evalue 0.05 -max_target_seqs 100 -outfmt 5 -taxids 562`
* All 5 examples validate against a faithful `JobSubmitRequest` reconstruction
  (mode_a → A, the other four → B); no schema failures.
* Patched build context confirmed to retain the example edits + VERSION 3.7.4
  and zero `ATGCATGC…` placeholders before `az acr build`.
* Image `elb-openapi:4.19` built from the dashboard-patched local context and
  pushed to ACR `acrelbdashboard3abp67bppe`, then rolled out to the running
  workload cluster `elb-cluster-02` (`rg-elb-cluster`).
* `uv run ruff check api/services/image_tags.py` + `uv run pytest -q
  api/tests/test_smoke.py` green.

## Rollout order

Followed the charter rule recorded in `api/services/image_tags.py`: build +
push the sibling image to ACR **first**, then move the pin in the dashboard.
The live AKS deployment image was rolled to `4.19` after the build succeeded.
