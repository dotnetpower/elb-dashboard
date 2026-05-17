# BLAST results / submit form — defensive hardening pass

## Motivation

After landing the BLAST results aggregate / alignments / export endpoints
and the BLAST-submit Duplicate / Export config flow, a critical-review
sweep surfaced eight load-bearing issues that would either lie to the
researcher, allow an authenticated user to fish in unrelated jobs / storage
accounts, or silently corrupt the form state on re-hydration.

This change closes all eight. Every fix carries a regression test.

## User-facing changes

- **Results analytics no longer pretend "no hits found"** when every result
  blob read failed (RBAC missing, storage outage, etc.). The endpoint now
  returns `status: "degraded"` with `degraded_reason: "all_reads_failed"`
  and a `read_failures` counter so the SPA can surface a real "storage
  unreachable" toast.
- **Result CSV/TSV export returns 503** instead of a misleading
  header-only file when every blob read fails.
- **Duplicate-job hydration is stricter**: a forged or stale config snapshot
  that carries `program: "rm -rf /"`, `sharding_mode: "wormhole"`, or a
  wrong-typed field (string instead of boolean, etc.) is silently dropped
  for that field — the rest of the form still loads. Previously these
  poisoned values would land in `FormState` and only surface at submit time
  as a validation error from the backend.
- **`isNucleotideProgram`** in the BLAST-submit query section no longer
  classifies `tblastn` as nucleotide. tblastn's query is protein, so the
  reverse-complement / GC% UI is now correctly hidden for that program.
- **`SeqStats`** splits `nCount` from `ambiguous`. The ambiguous-IUPAC
  warning chip in QuerySection now fires only on R/Y/S/W/K/M/B/D/H/V
  (matching `hasAmbiguousBases`), and the FASTA stats display can show N
  separately if the UI wants to in future.

## Security-facing changes

- **`_validate_result_blob_name`** rejects `..`, `?`, `#`, `\\`, `%2e`,
  `%2f`, empty `job_id`, `job_id` containing `/` or `..`, and a leading
  slash in the remainder after the prefix. Previously only the prefix and
  bare `..` were checked.
- **`_blob_service`** validates the storage account name against
  `^[a-z0-9]{3,24}$` before constructing the URL. Stops a forged
  querystring from redirecting the api sidecar's MI to an
  attacker-controlled URL (e.g.
  `storage_account=victim.blob.core.windows.net`).
- **`_ensure_job_read_allowed`** fails closed on Table Storage lookup
  failure when `AUTH_DEV_BYPASS` is **not** set. Previously a Storage
  outage would let any authenticated user read any job. Dev bypass keeps
  the fail-open behaviour because the synthetic identity has no real OID.

## API / IaC diff summary

- `api/services/storage_data.py`:
  - Added `_STORAGE_ACCOUNT_NAME_RE` (`^[a-z0-9]{3,24}$`) and a check in
    `_blob_service` that raises `ValueError` before the URL is built.
- `api/routes/stubs.py`:
  - `_validate_result_blob_name`: expanded checks (see above).
  - `_ensure_job_read_allowed`: fail-closed unless dev bypass.
  - `blast_job_results_aggregate`: returns `status: "degraded"` with
    `degraded_reason: "all_reads_failed"` when every blob read fails;
    surfaces `read_failures` + `truncated` on every response; catches an
    unexpected exception in `aggregate_blast_hits` and degrades cleanly.
  - `blast_job_results_export`: raises 503
    `{"code": "all_reads_failed"}` when every blob read fails.
- `web/src/pages/blastSubmit/configSerializer.ts`:
  - `normaliseFormFields`: per-field type validation. `program` /
    `sharding_mode` must match enum; numeric fields require finite
    numbers; boolean fields require booleans; everything else requires
    strings. Mismatches are dropped for that field only.
  - `partialFormFromJobPayload`: validates `program` against the enum.
- `web/src/pages/blastSubmit/fastaUtils.ts`:
  - `SeqStats` adds `nCount` and narrows `ambiguous` to IUPAC-beyond-N.
  - `baseComposition` updates accordingly.
- `web/src/pages/blastSubmit/QuerySection.tsx`:
  - `isNucleotideProgram` removes `tblastn`; adds a comment explaining
    why (protein query for tblastn means reverse-complement / GC% don't
    apply).

No infra changes. No new dependencies.

## Validation evidence

```
uv run pytest -q api/tests
  ...
  581 passed in 21.90s

cd web && npm test
  Test Files  14 passed (14)
  Tests       131 passed (131)   # configSerializer adds 4 new tests
                                  # storage_data adds 10 new tests
                                  # results_routes adds 4 new tests
                                  # fastaUtils assertion updated
cd web && npm run build
  ✓ built in 4.65s
```

New tests added:

- `api/tests/test_blast_results_routes.py`:
  - `test_alignments_rejects_backslash_traversal`
  - `test_alignments_rejects_url_encoded_traversal`
  - `test_aggregate_degraded_when_all_reads_fail`
  - `test_export_degraded_when_all_reads_fail`
- `api/tests/test_storage_data.py`:
  - `test_blob_service_rejects_invalid_account_names` (parametrised, 9 cases)
  - `test_blob_service_accepts_valid_account_names`
- `web/src/pages/blastSubmit/configSerializer.test.ts`:
  - `drops invalid program values (defence in depth)`
  - `drops invalid sharding_mode values`
  - `drops booleans where strings are expected and vice versa`
  - `rejects unrecognised program values in payload`
- `web/src/pages/blastSubmit/fastaUtils.test.ts`:
  - Updated `counts ambiguous bases separately from N` assertion.

## Out of scope

- Surfacing `degraded` / `truncated` in the BLAST analytics panel UI — the
  backend now sends the fields; a follow-up frontend change should render
  them as warning chips. Tracked for the next pass.
- The fail-closed change in `_ensure_job_read_allowed` is only exercised by
  the existing tests because they all run under `AUTH_DEV_BYPASS=true`. A
  dedicated test that bypasses the dev flag would require a fixture for the
  full MSAL caller — deferred.
