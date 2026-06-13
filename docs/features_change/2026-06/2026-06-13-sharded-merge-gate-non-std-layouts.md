---
title: Open the sharded merge gate to non-std extended tabular outfmt layouts
description: >-
  The sharded BLAST result merge now accepts any tabular outfmt 6/7 layout
  (including non-std-leading orders such as 7 qseqid sseqid staxids evalue
  bitscore) as long as it carries evalue + bitscore, mirroring the field-aware
  runtime merge across all gate layers.
tags:
  - blast
---

# Open the sharded merge gate to non-std extended tabular outfmt layouts

## Motivation

Issue [#29](https://github.com/dotnetpower/elb-dashboard/issues/29) items #2/#3.
The std-first canonical layout (`7 std staxids sscinames`) already shipped and
was verified live. But the gate still required `parts[1] == "std"`, so a
hand-written extended layout that leads with field codes
(`7 qseqid sseqid staxids sstrand pident evalue bitscore`) was rejected at submit
even though the runtime merge (`terminal/merge-sharded-results.sh`) is fully
field-aware — it resolves the group/rank/oracle columns BY NAME from the full
`-outfmt` specifier and only hard-requires `evalue` + `bitscore` (it raises a
clear `ValueError` otherwise). The submit gate now mirrors that exact rule.

## User-facing change

- A sharded BLAST submit accepts any tabular `outfmt 6`/`7` layout whose field
  list (with `std` expanded) includes both `evalue` and `bitscore` — including
  non-std-leading and reordered layouts. `qseqid` is optional (a missing query
  column makes the merge treat every hit as one query group, correct for a
  single-query search), matching the runtime merge.
- A tabular layout missing `evalue` or `bitscore` is now rejected at **submit
  time** with an actionable message ("The merge re-ranks shard hits by
  evalue/bitscore, so both columns are required.") instead of failing minutes
  later in the finalizer.
- `outfmt 5` (XML) still rejects extended fields. Single-token `5/6/7` and the
  std layouts are unchanged.

## API / IaC diff summary

- [api/services/sharding_precision.py](../../../api/services/sharding_precision.py)
  — `merge_format_for_outfmt` expands the field list (new `_expand_outfmt_field_codes`,
  mirroring the merge script's `_STD_TABULAR_FIELDS`) and accepts a tabular
  layout iff it carries `evalue` + `bitscore`; new `outfmt_spec_value` returns
  the FULL `-outfmt` specifier (not just the leading code).
- [api/services/blast/config.py](../../../api/services/blast/config.py) — the
  sharding gate uses `outfmt_spec_value` (full specifier) and a clearer error
  message.
- [terminal/patch_elastic_blast.py](../../../terminal/patch_elastic_blast.py) —
  `patch_partitioned_outfmt_gate` drops the per-code `startswith('std')`
  restriction so the elastic-blast partitioned gate allows any tabular `6`/`7`
  layout on both the internal and OpenAPI planes. Baked into `elb-openapi:4.23`.
- [api/services/image_tags.py](../../../api/services/image_tags.py) —
  `IMAGE_TAGS["elb-openapi"]` `4.22` → `4.23`.

## Validation evidence

- `uv run pytest -q api/tests/test_sharding_precision.py
  api/tests/test_blast_config_sharding.py
  api/tests/test_terminal_patch_elastic_blast.py` — passing. New tests:
  `test_merge_format_accepts_extended_non_std_layout_with_evalue_bitscore`,
  `test_merge_format_blocks_layout_missing_rank_columns`,
  `test_outfmt_spec_value_rejoins_unquoted_multi_token`,
  `test_sharded_accepts_non_std_extended_layout_with_rank_columns`,
  `test_sharded_rejects_tabular_layout_missing_rank_columns`, and the widened
  `test_patch_partitioned_outfmt_gate_allows_outfmt7`.
- `elb-openapi:4.23` rebuilt from the patched sibling context (ACR run `deg7`)
  and redeployed; the partitioned gate widening verified live in the pod.
