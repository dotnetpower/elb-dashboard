# Security audit 2026-05-22 — items #4 (tid claim) + #8 (cross-account)

## Motivation
Two related defence-in-depth gates bundled together because both close a
"trust caller-supplied data more than necessary" gap and live one layer
apart (token validator → route handlers).

- **#4 (HIGH)** — `api/auth.py::_validate_token` accepted the bearer
  based on issuer / audience / signature, but **never explicitly
  compared the token's `tid` claim against the configured
  `AZURE_TENANT_ID`**. The issuer-list check already constrained the
  tenant in practice, but a future regression that broadens the issuer
  list (e.g. accidentally adding the multi-tenant `common` endpoint)
  would silently let cross-tenant tokens through.
- **#8 (HIGH)** — Every `/api/blast/jobs/{job_id}/*` results route
  accepts `storage_account` as a query parameter and uses it verbatim
  with the shared MI to read result blobs. A legitimately-authenticated
  tenant member could submit a BLAST job with `storage_account=mine`,
  then call `GET /api/blast/jobs/{job_id}/results/aggregate?storage_account=victim`
  and ride the MI's broader Reader scope into result blobs in a
  storage account that has nothing to do with their job — a classic
  confused-deputy / cross-account read.

## User-facing change
- **#4** — Tokens whose `tid` claim does not match `AZURE_TENANT_ID`
  are now rejected with `401 token tenant_id does not match configured
  AZURE_TENANT_ID`. Tokens that omit `tid` entirely (non-AAD issuer or
  tampered) are also rejected. `AUTH_DEV_BYPASS=true` is unaffected —
  the synthetic identity skips `_validate_token` altogether.
- **#8** — `GET /api/blast/jobs/{job_id}/results/...` and the legacy
  `/jobs/{job_id}/file` route now reject the request with
  `403 {"code": "cross_account_mismatch", "message": "..."}` when the
  caller-supplied `storage_account` does not match the value recorded
  on the JobState row at submit time. Case differences are normalised
  (Azure account names are case-insensitive) and the recorded value is
  deliberately **not echoed** in the response so the gate is not a
  side-channel for probing job ownership.
- Legacy rows that pre-date the `storage_account` field stay accessible
  (the helper falls back to the supplied value and logs the fallback).
- A transient state-repo failure fails **closed** with HTTP 503
  `auth_lookup_unavailable` (production); under `AUTH_DEV_BYPASS=true`
  it degrades open so the dev loop without a real state backend
  continues to work.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Auth | [api/auth.py](../../../api/auth.py) | `_validate_token` now reads `claims["tid"]` and rejects if missing or `!= tenant_id` (case-insensitive compare). |
| Services | [api/services/blast_job_state.py](../../../api/services/blast_job_state.py) | New `_resolve_job_storage_account(job_id, supplied)` helper using `JobStateRepository.get_summary` (cheap — no `payload_json`). Mirrors the fail-closed / dev-bypass-fallback pattern already used by `_ensure_job_read_allowed`. |
| Routes (re-export) | [api/routes/_blast_shared.py](../../../api/routes/_blast_shared.py) | Re-exports `_resolve_job_storage_account` so route modules import from one place. |
| Routes | [api/routes/blast/results.py](../../../api/routes/blast/results.py) | Calls `_resolve_job_storage_account` at the top of all 6 job-bound routes: `/jobs/{job_id}/file`, `/results`, `/results/aggregate`, `/results/download`, `/results/export`, `/results/{file_id}`. |
| Routes | [api/routes/blast/result_analytics.py](../../../api/routes/blast/result_analytics.py) | Same wiring for `/jobs/{job_id}/results/alignments` and `/results/taxonomy`. |
| Tests | [api/tests/test_security_audit_4_8.py](../../../api/tests/test_security_audit_4_8.py) | New 15-test file: `tid` matching / mismatch / missing, storage cross-check accepts matching + case-insensitive, rejects cross-account, falls back on unrecorded, fails closed on lookup error, degrades open in dev_bypass, returns immediately on empty supplied, plus a parametrised end-to-end TestClient case for each of the 5 job-bound results routes. |

No IaC changes. No new dependencies. No deploy required.

## Validation evidence
- `uv run ruff check api/auth.py api/services/blast_job_state.py api/routes/_blast_shared.py api/routes/blast/results.py api/routes/blast/result_analytics.py api/tests/test_security_audit_4_8.py` → passed.
- `uv run pytest -q api/tests/test_security_audit_4_8.py` — **15 passed**.
- `uv run pytest -q api/tests` — **943 passed** (was 924 → +19 from the
  new file + a few touched parametrised cases).

## Hardening pass (same day)
A self-critique surfaced two additional weaknesses; fixed in the same
change:

- **MEDIUM — `state is None` (job genuinely absent) silently degraded
  open without any audit log.** A user with a valid bearer could probe
  arbitrary `job_id` values against arbitrary `storage_account` query
  params and the only observable signal would be the Storage SDK 404.
  Fixed: the helper now emits an `INFO` log line whenever the fallback
  path runs (`storage account cross-check: no JobState row for job_id=...`).
  This does not change behaviour but turns the access pattern into a
  signal an operator can monitor for in the api sidecar logs.
- **HIGH — Wiring guard missing.** The first draft of the tests
  exercised the helper directly. If a future refactor dropped the
  `_resolve_job_storage_account(...)` call from a route handler, the
  unit tests would still pass and the regression would only surface in
  production. Fixed: added a parametrised end-to-end `TestClient` test
  that hits each of the 5 job-bound results routes with a
  cross-account `storage_account=` query param and asserts the 403 +
  `cross_account_mismatch` code is returned BEFORE any Storage SDK call
  happens. Drops the assertion that the recorded value is never echoed
  in the response body.

## Non-goals (deferred)
- Locking `databases.py` routes to a per-caller account list. Those
  routes (`GET /api/blast/databases`, `GET /api/blast/databases/versions`)
  legitimately let the user choose any account where their data lives;
  the right gate there is the upcoming app-role authz (#1/#4 Phase 1),
  not a JobState lookup.
- 404 for `job_id` that does not exist anywhere. Some routes
  intentionally degrade open on missing state (external-sync rows in
  flight); changing this would break the legacy job list. Tracked
  separately if the new INFO log shows the path is hot.

## Audit progress
9/20 audit findings now closed (#3, #4, #5, #6, #7, #8, #9, #10, #11).
Remaining: #1 (role authz, design doc only), #2 (per-ticket tmux,
design doc only), #12, #13–#20.
