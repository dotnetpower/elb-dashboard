# Fix: `/api/blast/databases/nt/preview` 502 — URL-encode NCBI S3 continuation token

## Motivation

Production users hit a consistent HTTP **502 Bad Gateway** when opening the
`nt` database preview modal on the BLAST Databases page. The
`/api/blast/databases/nt/preview` route was returning 502 in ~1.5 s on every
attempt, while smaller DBs (`pdb`, `swissprot`, `core_nt`, `nr`, …) worked
fine.

Root cause: in `_list_keys` (`api/routes/storage/common.py`), the AWS S3
`NextContinuationToken` was concatenated **unencoded** into the
`continuation-token=` query parameter:

```python
list_url += f"&continuation-token={continuation}"
```

S3 pagination tokens are opaque base64-ish blobs and routinely contain `+`
and `/`, both URL-significant characters. Without percent-encoding, S3
rejected page 2 with:

```
HTTP 400 InvalidArgument
The continuation token provided is incorrect
```

`resp.raise_for_status()` raised `httpx.HTTPStatusError`, the outer
`except httpx.HTTPError` wrapped it as `NcbiUnavailable`, and the route
translated that to HTTP 502.

Only DBs whose listing actually paginated (>1000 objects, i.e. `nt` and
sometimes `nr`) triggered the bug. Single-page DBs never reached the
second `client.get(...)` call, which is why most previews looked healthy.

Confirmed by reproduction:

```text
HTTP 400 InvalidArgument
The continuation token provided is incorrect
ArgumentName: continuation-token
```

## User-facing change

* The BLAST Databases preview modal (and any background snapshot check that
  relies on `preview_database`) now returns the full file count, snapshot id,
  signature ETag and estimated size for paginated DBs such as `nt`.
* The route response shape is **unchanged** — only the success path is
  restored.

## API / IaC diff summary

* `api/routes/storage/common.py` — `_list_keys` now percent-encodes the
  S3 continuation token with `urllib.parse.quote(continuation, safe='')`.
  Token is treated as opaque; `safe=''` also encodes `/`.
* `api/routes/blast/databases.py` — `blast_database_preview` warning log now
  includes the actual `NcbiUnavailable` / `NcbiAccessDenied` message, not
  just the exception class name. Previously the only log line was
  `"preview nt: NCBI unavailable: NcbiUnavailable"`, which hid the
  underlying S3 400 body and made this bug effectively invisible.
* `api/tests/test_storage_common_cache.py` — adds
  `test_list_keys_url_encodes_continuation_token` regression test that
  asserts a token containing `+` and `/` is percent-encoded before it
  reaches the second-page request.

No IaC changes. No frontend changes. No sidecar layout changes.

## Validation evidence

* `uv run pytest -q api/tests/test_storage_common_cache.py api/tests/test_ncbi_catalogue.py api/tests/test_ncbi_breaker_composite.py api/tests/test_blast_databases_preview.py api/tests/test_prepare_db_routes.py`
  → **21 passed**.
* `uv run ruff check api/routes/storage/common.py api/routes/blast/databases.py api/tests/test_storage_common_cache.py`
  → **All checks passed!**
* Reproduction of bug against live NCBI S3 with an unencoded token returned
  `HTTP 400 InvalidArgument — The continuation token provided is incorrect`;
  the same request with `urllib.parse.quote(token, safe='')` succeeds.
* Production failure observed in Log Analytics workspace
  `log-elb-dashboard-3abp67bppeeg4`, container `api`, ~05:28 UTC
  2026-05-26: multiple `GET /api/blast/databases/nt/preview` returning
  `status=502 elapsed=~1.5 s`, each preceded by
  `"preview nt: NCBI unavailable: NcbiUnavailable"`.
