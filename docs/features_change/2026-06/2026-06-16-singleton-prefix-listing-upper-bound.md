---
title: Fix singleton prefix-listing upper bound (drops keys above the sentinel)
description: list_singletons_by_prefix used a fixed trailing-tilde sentinel that silently excluded rows whose suffix sorted at/above it; derive the range upper bound from the prefix so every matching row is returned regardless of suffix charset.
tags:
  - operate
  - architecture
---

# Fix singleton prefix-listing upper bound

## Motivation

`api/services/state/singletons.py::list_singletons_by_prefix` built its Azure
Table range query upper bound as `sanitised_prefix + "~~~~~~~~"` (eight `~`,
0x7e). A fixed trailing sentinel is incorrect: it silently **excludes** any row
whose suffix sorts at or above the sentinel. Two concrete classes are dropped:

* a suffix containing eight or more `~` after the prefix, and
* any suffix whose first differing character is **above** `~` — RowKeys may
  legitimately contain such characters because the key sanitiser only strips
  `/ \ # ?` and `\u0000-\u001f` / `\u007f-\u009f`, so e.g. accented letters
  (`\u00e9`) and other code points ≥ `\u00a0` survive into the stored key.

The comment claimed a "`chr(0x7e+1)` trick", but the code did not implement it,
and even that intent would have been wrong for keys carrying characters ≥
`\u0080`.

The only caller today is the OpenAPI public-HTTPS reconciler, whose per-cluster
keys are `openapi:runtime:public-base-url:cluster:<sha256[:16]>` — pure hex, so
the defect never bit in production. It is still a latent correctness bug: any
future singleton with a different key shape could silently lose rows.

## User-facing change

None observable today (the live keys are hex digests). This is an internal
correctness fix that makes the storage primitive honour its documented contract
("return every row whose key starts with `prefix`") for any suffix charset.

## API / IaC diff summary

* `api/services/state/singletons.py`:
  * New `_prefix_upper_bound(prefix)` — returns the smallest string strictly
    greater than every string starting with `prefix` by incrementing the last
    code point of the prefix. Correct for any suffix because all matching keys
    share the prefix and therefore sort below the incremented-prefix bound,
    independent of what follows.
  * `list_singletons_by_prefix` now uses `_prefix_upper_bound(sanitised_prefix)`
    for the `RowKey lt` bound and guards an empty sanitised prefix.
* `api/tests/test_state_singletons.py`:
  * `_FakeTableClient.query_entities` parses the `PartitionKey eq / RowKey ge /
    RowKey lt` filter and applies an ordinal string comparison (matching Azure
    Tables) so the bound math is exercised, not a wildcard.
  * `test_list_by_prefix_returns_matching_rows` — only the prefixed rows return.
  * `test_list_by_prefix_includes_suffix_above_tilde` — regression guard: a
    nine-`~` suffix and a `\u00e9`-prefixed suffix are both returned (both were
    dropped by the old sentinel).
  * `test_list_by_prefix_empty_prefix_returns_empty`.

## Validation evidence

* `uv run pytest -q api/tests/test_state_singletons.py` → 8 passed (3 new).
* Consumer sweep: `test_openapi_public_https_reconcile.py`,
  `test_openapi_runtime_endpoint_durable.py`,
  `test_openapi_runtime_token_cache.py` → 30 passed.
* Full backend: `uv run pytest -q api/tests` → 3840 passed, 3 skipped.
* `uv run ruff check` on both touched files → clean.
