---
title: Sequence-diversity result selection
description: Add an opt-in, bounded aligned-sequence grouping policy for partitioned tabular OpenAPI and Service Bus jobs without changing existing result policies.
tags:
  - blast
  - architecture
  - user-guide
---

# Sequence-diversity result selection

## Motivation

The existing result policies select subject accessions. Some integrations need
one representative HSP for each distinct aligned subject sequence and query
span before their own accession-level collapse and union-coverage filters.
Applying that behavior implicitly would change established result membership
and ordering, so it is a separate opt-in policy.

## User-facing change

Partitioned tabular requests can set
`blast_options.result_selection_policy=sequence_diversity`. Omitted policy,
explicit `native_top_n`, and `diversity_aware` retain their existing output.

The versioned signature is:

```text
sequence_identity_mode = aligned_sequence_query_span
sequence_identity_version = 1
signature = (query identity, uppercase(sseq with ASCII '-' removed), qstart, qend)
```

Ambiguity symbols and whitespace are compared literally after uppercasing.
There is no reverse-complement normalization and no coordinate reordering.
Accession is not part of the signature, so one group may contain several
accessions and one accession may occur in several groups.

`max_target_seqs` is the final per-query group count. The optional
`candidate_pool_size` is a separate per-shard subject cap with server default
`2000` and no fixed server maximum. Explicit values remain finite per-request
bounds and must be positive and at least the requested group count. Values
below the requested group count or attached to another policy are rejected
instead of clamped. Larger pools increase BLAST output, storage, and merge work.

The policy supports outfmt 6/7 only. Callers provide query identity, accession,
`sseq`, `qstart`, `qend`, `evalue`, and `bitscore`; server enrichment adds
`score` before validation. XML and missing semantic fields return typed HTTP
422 responses.

## API and runtime summary

- The [OpenAPI](https://www.openapis.org/) schema adds `sequence_diversity` and
  `candidate_pool_size` without changing defaults.
- The merger stores row metadata and sequence signatures in file-backed
  SQLite, selects representatives with the existing e-value/raw-score/source
  order comparator, and writes only representative rows for this policy.
- The finalizer requires every expected shard before creating the canonical
  merge. Partial shard output never becomes a completed result.
- Malformed or incomplete candidate rows fail the sequence merge instead of
  being omitted from a seemingly complete canonical result.
- The merge report adds requested/applied policy, identity metadata, observed
  candidate/group counts, requested/applied pool size, shard completion,
  saturation, and bounded shortfall reasons.
- Per-group report detail is capped at 5,000 entries with an explicit
  truncation flag; canonical results and aggregate counts are unaffected.
- `observed_pool_complete` means no observed shard/query set reached the cap;
  it does not mean the full database was exhausted.
- The runtime has no stable deduplicated exact-HSP identity beyond source-row
  ordinal. It reports accession and source-row counts per group and omits
  `sequence_group_hsp_count` rather than inventing a count.
- Existing `full`, `merged`, and `xml` result-download modes are unchanged.
  One job has one policy-applied canonical result.

Sequence selection happens before downstream accession collapse, union-coverage
calculation, and identity/coverage filters. A downstream result is therefore
not guaranteed to retain N accessions, and group counts cannot reconstruct
accession-level union coverage.

## Service Bus behavior

Local validation and permanent sibling HTTP 422 responses preserve the
sequence-specific code in the durable `blast.transition` failure event and the
[Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
dead-letter reason. The event has an empty `openapi_job_id`, retains
`external_correlation_id` and `request_id`, and adds `retryable=false`. The
message is not permanently dead-lettered until the response event is durable.
Existing malformed requests continue to use `servicebus_malformed_request`.
Sequence-specific permanent 409/422 responses retain their upstream code. A
sequence request validates before idempotency replay, and a key bound to
different policy, group-count, or pool semantics fails with
`sequence_diversity_idempotency_conflict` rather than returning a mismatched
existing result.

## Validation

- All `185` sibling OpenAPI tests pass in an isolated environment built from
  the sibling's declared runtime and development requirements.
- The merge matrix covers unchanged existing policies, signature
  normalization, literal ambiguity handling, no reverse-complement folding,
  repeated accessions, query spans, cross-shard groups, representative order,
  shortfalls, zero results, saturation, missing shards, and report counts.
- A 6,000-row synthetic candidate pool with long aligned-sequence fields passes
  under a 96 MiB address-space limit using the disk-backed merge path.
- Dashboard Pydantic, Service Bus translation, durable failure-event, DLQ,
  result-artifact, split-report, runtime-patcher, and local merge tests pass.
- The full Dashboard backend suite passes with `5,836 passed, 4 skipped`; the
  skipped checks require external parity evidence directories. Subprocess merge
  and slow coverage passes with `137 passed`.
- Ruff, the mypy debt ratchet, the 242-operation OpenAPI contract check,
  generated TypeScript drift check, docs frontmatter guard, and strict MkDocs
  build pass.
- The dashboard patcher applies twice to a fresh sibling build-context copy and
  leaves no source diff.
- The customer response HTML renders without horizontal overflow at 1440 x
  1000 and 390 x 844, with no console or page errors.

No [Azure](https://azure.microsoft.com/) image was built or deployed. The live
`elb-openapi:4.55` runtime is unchanged; `elb-openapi:4.56` remains a future
source target pinned to sibling commit
`142b9cea0629bdee4c325fa4af56ed77a72983d8`, published on the sibling remote
`master` branch before the Dashboard commit was pushed.