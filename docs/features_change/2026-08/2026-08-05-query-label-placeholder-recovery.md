---
title: Stop the Query ID header contradicting the FASTA preview
description: The generic "query.fa" placeholder is truthy, so it permanently blocked the durable Storage-backed defline recovery. External jobs showed "Query ID query.fa" while the prepare-step preview below showed the real defline.
tags: [blast, ui]
---

# Stop the Query ID header contradicting the FASTA preview

## Motivation

On a Service Bus / OpenAPI job the Run details page showed two contradictory
facts about the same file:

| Surface | Showed |
| --- | --- |
| Header `Query ID` | `query.fa` |
| Prepare Run step, `input.fa` preview | `>warmup` |

Both are honest reads of different sources, which is exactly what made it
misleading — a reader cannot tell which one is the real query identity.

* The **preview** streams the actual blob (`queries/<openapi_id>.fa`) through
  the api sidecar, so it always shows the true defline.
* The **header** reads `query_label`. External jobs carry no query identity on
  the sibling job record, so the defline is bridged through ephemeral OPS Redis
  (`external_query_labels`). Redis is an in-revision sidecar — every Container
  App revision restart wipes it. After that the projection substitutes the
  generic `"query.fa"` filename placeholder.

A durable recovery already existed (`derive_external_query_label` reads the
first defline from the query blob), but its trigger was
`if not str(out.get("query_label") or "").strip()`. `"query.fa"` is **truthy**,
so once that placeholder was persisted onto the `jobstate` row the recovery
could never fire again. The same "fill only when empty" bug existed in the
list-sync heal path (`not cur_query`), so the Recent searches list was stuck on
`query.fa` permanently too.

## User-facing change

The header now shows the real defline (`warmup` in the reported case) and
agrees with the FASTA preview below it. Recent searches converges on the same
value on the next poll.

## API / IaC diff summary

No route, schema, or infrastructure change. Behaviour only.

* `api/services/blast/external_query_labels.py` — new `GENERIC_QUERY_LABELS`
  (`query.fa`, `input.fa`) and `is_generic_query_label()`, the single authority
  for "is this a real query identity or a display placeholder?".
* `api/routes/blast/jobs.py` — the detail-view recovery trigger uses that
  predicate, and the recovered label is now persisted with
  `repo.update(..., query_label=...)` in addition to the Redis re-remember.
* `api/services/blast/external_jobs.py` — the list-sync heal treats a stored
  placeholder as absent, matching the existing `program` / `job_title` heal.
* `api/services/blast/job_state.py` — extracted the ownership-free
  `_query_blob_path_from_payload()`; `derive_external_query_label` now checks
  "is this an external job?" **before** resolving the blob path, and authorises
  through `_assert_job_owner` (which honours `BLAST_JOBS_SHARED_VISIBILITY`)
  instead of the stricter inline check in `_job_query_blob_path`.

### Cost

Bounded, and lower than before in steady state:

* Detail view only — never the list path.
* One 512-byte capped Storage read, and only when the row still carries a
  placeholder.
* The result is written back to the Table row, so the read does not repeat for
  that job. A row with a real defline pays nothing (locked by
  `test_job_detail_keeps_real_query_label_without_storage_read`).

### Bug found while fixing

Widening the trigger surfaced a latent 403: `derive_external_query_label` went
through `_job_query_blob_path`, whose inline owner check ignores
`BLAST_JOBS_SHARED_VISIBILITY`. With shared visibility on, opening another
user's job detail would have returned 403 from a display-only code path.
Caught by the existing `test_job_detail_allows_other_owner_when_flag_on` and
fixed by the reordering above.

## Validation

```
uv run ruff check api                 # All checks passed!
uv run pytest -q api/tests            # 5000 passed, 3 skipped
```

New tests:

* `test_is_generic_query_label_true_for_placeholders` /
  `..._false_for_real_deflines` — the predicate, including case and whitespace.
* `test_job_detail_recovers_query_label_over_generic_placeholder` — a row
  stored as `query.fa` recovers `warmup` from `queries/openapi-ph.fa` and
  persists it via `repo.update`.
* `test_job_detail_keeps_real_query_label_without_storage_read` — no Storage
  read when the row already has a real defline.
* `test_sync_external_jobs_heals_placeholder_query_label` — the list sync heals
  a stored placeholder.
