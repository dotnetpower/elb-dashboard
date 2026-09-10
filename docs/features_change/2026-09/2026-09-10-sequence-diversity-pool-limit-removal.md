---
title: Remove the sequence-diversity pool hard limit
description: Accept positive finite sequence-diversity candidate pools above the former 5,000-entry server limit while retaining request-level and disk-backed resource bounds.
tags:
  - blast
  - architecture
  - user-guide
---

# Remove the sequence-diversity pool hard limit

## Motivation

The initial opt-in sequence-diversity contract rejected candidate pools above
5,000 subjects per shard. That fixed server limit prevented callers from
choosing a wider observed candidate window even though the merge implementation
already stores row bodies on disk and ranking metadata in
[SQLite](https://www.sqlite.org/).

## User-facing change

The [OpenAPI](https://www.openapis.org/) and Dashboard Service Bus validation
paths now accept any positive integer `candidate_pool_size` greater than or
equal to `max_target_seqs`. Values above 5,000 are no longer rejected. Omitting
the field still applies the `2000` default, expanded to `max_target_seqs` when
that requested group count is larger.

Each explicit pool remains a finite per-request and per-shard BLAST subject
bound. Larger values increase shard output, storage, and merge work; this change
does not turn candidate collection into an in-memory unbounded list. Existing
policies, request defaults, result ordering, download modes, typed error codes,
and idempotency semantics are unchanged.

The merge report retains a separate 5,000-entry limit for per-group detail and
sets `sequence_group_counts_truncated=true` when needed. That observability
limit does not truncate canonical output or aggregate group counts.

## API and runtime diff

- Removed the fixed maximum from both request schemas and positive-integer
  validators.
- Removed the terminal merge boundary rejection above 5,000.
- Updated the ElasticBLAST config patch to accept non-negative transport values
  and migrate an already-patched `0..5000` validation fragment idempotently.
- Hardened the Dashboard build-context validator to reject sibling source that
  still contains the legacy cap.
- Advanced the immutable sibling source pin to commit
  `787b1939c334d33d37cd9b7f37da470411e027e7`, including the follow-up merge
  publication hardening.
- No infrastructure or deployed image changed.

## Validation

- The full sibling OpenAPI suite passes with `188 passed`.
- The full Dashboard backend suite passes with `5,837 passed, 4 skipped`; the
  skipped tests require external parity evidence directories.
- Dashboard slow and subprocess coverage passes with `150 passed`.
- A 6,000-row candidate pool with 16 KiB aligned-sequence values returned all
  6,000 groups under a 96 MiB address-space limit through the file-backed merge
  path; only the 5,000-entry report-detail list was marked truncated.
- The default 2,000 pool and explicit 20,000 pool both propagate through sibling
  submit planning, generated ElasticBLAST config, and persisted job state.
- Ruff, the production mypy debt ratchet, the 242-operation OpenAPI contract
  check, generated TypeScript drift check, docs frontmatter guard, and strict
  MkDocs build pass.
- The updated customer response renders without horizontal overflow at
  1440 x 1000 and 390 x 844, with no console or page errors.
- No Azure image was built or deployed.