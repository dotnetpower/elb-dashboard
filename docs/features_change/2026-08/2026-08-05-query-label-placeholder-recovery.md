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
  for "is this a real query identity or a display placeholder?"; plus the
  `remember_query_label_miss()` / `query_label_miss_recorded()` negative marker
  and `_clean_label_token()` masking.
* `api/services/blast/query_label_recovery.py` — **new module** owning the whole
  recovery decision (guard order, persist, marker). The route keeps only HTTP
  shaping, per the charter SRP gate.
* `api/routes/blast/jobs.py` — three lines: call the recovery helper and apply
  the result.
* `api/services/blast/external_jobs.py` — the list-sync heal treats a stored
  placeholder as absent, matching the existing `program` / `job_title` heal.
* `api/services/blast/job_state.py` — extracted the ownership-free
  `_query_blob_path_from_payload()` and the pure `external_payload_of()`;
  `derive_external_query_label` now takes the ALREADY-LOADED `JobState` and
  authorises through `_assert_job_owner` (which honours
  `BLAST_JOBS_SHARED_VISIBILITY`) instead of the stricter inline check in
  `_job_query_blob_path`.

### Cost

The detail route is **polled**, so every branch is bounded:

| Case | Cost |
| --- | --- |
| Dashboard job (`query.fa` is also its own upload basename) | 0 — pure dict lookup, no Redis, no Storage, no extra Table read |
| External job, first poll, defline found | 1× 512-byte ranged Storage read + 1 Table MERGE, then the guard is False forever |
| External job, blob missing / header-less / defline == placeholder | 1× Storage read per 600 s negative-marker TTL |
| Row write failed | negative marker bounds the retry; this response still shows the recovered label |
| OPS Redis down | markers no-op → recovery re-runs, i.e. the pre-existing cost profile |

A row that already carries a real defline pays nothing (locked by
`test_job_detail_keeps_real_query_label_without_storage_read`).

### Hardening round findings (fixed in this change)

| Severity | Finding | Fix |
| --- | --- | --- |
| Critical | `repo.update()` defaults `updated_at` to *now*. The SPA renders "Runtime · Workflow" as `created_at → updated_at`, so a display-only backfill would have inflated a finished job's elapsed time to "time until someone first opened the detail page". | Echo `state.updated_at` back explicitly. |
| High | Widening the guard made the recovery fire for **dashboard** jobs too — `canonical_job_metadata` stores the basename, so `uploads/<job>/query.fa` is persisted as `query.fa`. Every poll of every dashboard job would have paid a Table re-read plus a Redis round trip. | `external_payload_of()` guard runs first, and `derive_external_query_label` now takes the already-loaded row. |
| High | A blob that is missing, unreadable, or header-less can never fill the positive cache, so a polled tab re-paid a Storage read forever. | Short-TTL negative marker in its own Redis namespace. |
| High | A defline that is literally `>query.fa` derives back into the placeholder — accepting it re-entered the same branch on the next poll (Storage read + Table write + history row per poll). | Treat a generic recovered label as a miss. |
| High | `derive_external_query_label` went through `_job_query_blob_path`, whose inline owner check ignores `BLAST_JOBS_SHARED_VISIBILITY`. With shared visibility on, opening another user's job detail returned 403 from a display-only path. | Authorise through `_assert_job_owner`; check "is external?" before resolving the blob. |
| Medium | The defline is attacker-controlled (any API caller picks it, and the recovery re-reads it from Storage), yet it is persisted on the row and rendered in the header and Recent searches. A `>https://…?sv=…&sig=…` defline would have leaked a SAS into the UI (charter §12). NUL bytes would also break the Table write. | `_clean_label_token()` runs `sanitise()` + strips control characters at the single derivation point, so the submit-time bridge and the Service Bus message subject are covered too. |
| Medium | A durable row mutation with no log line — `query_label` would silently change between two list responses with nothing to grep for. | One-shot `LOGGER.info` on the persist. |
| Medium | ~90 lines of Redis + Storage + Table orchestration inline in an already-large route function. | Extracted to `api/services/blast/query_label_recovery.py`. |

## Validation

```
uv run ruff check api                 # All checks passed!
uv run pytest -q api/tests            # 5010 passed, 3 skipped
uv run mypy api/services/blast/query_label_recovery.py   # clean (new module)
uv run python scripts/docs/check_frontmatter.py
DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict
```

New tests:

* `test_is_generic_query_label_true_for_placeholders` /
  `..._false_for_real_deflines` — the predicate, including case and whitespace.
* `test_derive_masks_a_sas_url_in_the_defline`,
  `test_derive_strips_control_characters`,
  `test_derive_returns_empty_when_only_control_chars`,
  `test_derive_still_caps_after_masking` — label sanitisation.
* `test_query_label_miss_marker_round_trip` / `..._best_effort_on_redis_failure`
  / `..._ignores_blank_job_id` — the negative marker, including that it stays
  out of the `recall_query_label` namespace.
* `test_job_detail_recovers_query_label_over_generic_placeholder` — a row
  stored as `query.fa` recovers `warmup`, persists it, and preserves
  `updated_at`.
* `test_job_detail_keeps_real_query_label_without_storage_read` — no Storage
  read when the row already has a real defline.
* `test_job_detail_query_label_miss_is_bounded_by_negative_marker` — three
  polls, one Storage read.
* `test_job_detail_self_referential_defline_does_not_loop` — three polls, one
  Storage read, zero row writes.
* `test_job_detail_dashboard_job_placeholder_costs_no_io` — a dashboard job
  touches neither Storage nor Redis and reads the row exactly once.
* `test_sync_external_jobs_heals_placeholder_query_label` — the list sync heals
  a stored placeholder.

### Known pre-existing issue (not changed here)

`_maybe_recover_external_failure_error` in the same route calls
`repo.update(state.job_id, error_code=…)` without passing `updated_at`, so it
has the same timestamp-inflation defect fixed above. It fires only for a failed
external row that carries no `error_code`, and it is outside this change's
scope — recorded here so it is not rediscovered from scratch.

