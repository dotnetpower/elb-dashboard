---
title: Correct the outfmt guidance to the UNQUOTED canonical multi-token form
description: >-
  The backend rejection message for a multi-token outfmt field now recommends
  the UNQUOTED additional_options form (-outfmt 7 std staxids sscinames) instead
  of the quoted form, which broke the generated Job YAML.
tags:
  - blast
---

# Correct the outfmt guidance to the UNQUOTED canonical multi-token form

## Motivation

Issue [#29](https://github.com/dotnetpower/elb-dashboard/issues/29) item #1. The
backend guidance in `api/services/blast/config.py` rejected a multi-token value
in the `outfmt` field and told the user to pass the extended layout via
`additional_options` **with quotes** (`-outfmt "7 std staxids ..."`). That quoted
form is wrong: the sibling runtime injects `ELB_BLAST_OPTIONS` into the generated
K8s Job YAML via a raw `${VAR}` regex substitution with **no YAML escaping**
(`value: "${ELB_BLAST_OPTIONS}"`), so a quoted value produces
`value: "-outfmt "7 std staxids ..."` and breaks `kubectl apply` ~60 s later in
the cluster.

The canonical wire format is the **UNQUOTED** multi-token specifier. The deployed
`blast-run-aks.sh` rebuilds `ELB_BLAST_ARGV` and rejoins the `-outfmt` tokens up
to the next `-flag` into a single blastn argument, so the unquoted form survives
the shell word-split (verified live on the OpenAPI plane, see the elb-openapi 4.22
rebuild note).

## User-facing change

- The 422 message for a multi-token `outfmt` field value now recommends:
  > pass it via additional_options UNQUOTED as `-outfmt 7 std staxids sscinames`
  > (do not add quotes; quotes break the generated Job YAML). Lead with std so the
  > qseqid column stays first.
- The `outfmt` field still accepts only a single format code (e.g. 5, 6, 7); the
  two-path rule is unchanged. Only the recommended shape for `additional_options`
  changed from quoted to unquoted.

## API / IaC diff summary

- [api/services/blast/config.py](../../../api/services/blast/config.py) —
  reworded the multi-token rejection message + comment to the UNQUOTED canonical
  form.
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_config_sharding.py` — 48 passed.
  New test `test_outfmt_field_rejection_recommends_unquoted_form` asserts the
  message contains `UNQUOTED` and `do not add quotes`;
  `test_extended_outfmt_via_additional_options_is_accepted` and
  `test_sharded_merge_allows_tabular_std_outfmt` updated to the unquoted shape.
