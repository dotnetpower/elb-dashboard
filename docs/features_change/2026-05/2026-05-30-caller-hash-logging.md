# 2026-05-30 — caller_hash on the request-completion log line (audit P3 #26)

## Motivation

Audit P3 #26 flagged that the per-request completion line emitted by
`api/app/middleware.py::RequestIdMiddleware`
(`req rid=... method=... path=... status=... elapsed=...ms`) carried no
identity field at all. The dashboard's request-detail panel and any
downstream KQL query had no way to count traffic per caller without
parsing the bearer token a second time — and any quick-and-dirty fix
that stamped the raw `oid` / `upn` on the line would have leaked
identifiers into the standard log stream.

The fix adds a single `caller_hash=<sha256-prefix>` token to both the
success path (`req rid=...`) and the failure path (`req_failed rid=...`)
of the middleware. The hash is produced by the existing
`api.services.sanitise.redact_oid` helper (same family as PR-1's PII
log redaction and PR-7's `STRICT_AUDIT_HASH`), so an operator can
deterministically join "all requests by user X across the last hour"
without ever recovering the raw OID.

This is purely additive observability — no behavioural change — so it
is **not** gated behind a `STRICT_*` env var (§12a Rule 4 explicitly
scopes the default-OFF requirement to "new positive validation"
changes; an extra log token is observability, not validation).

## User-facing change

* Every middleware completion line now ends with
  `caller_hash=<sha256-prefix>`. Anonymous requests render as
  `caller_hash=None` so the token shape is consistent for log shippers.
* `api/app/jwt_utils.py` gains `_decode_jwt_oid(authz)`, the OID/sub
  twin of the existing `_decode_jwt_upn(authz)`. Same "best-effort,
  display-only, NEVER auth" contract — payload is base64-decoded
  without signature verification because the route's own
  `require_caller` dependency remains the real auth gate.
* The shared base64 path is now extracted into `_decode_jwt_payload`
  so the two helpers cannot drift apart on malformed-token handling.

## API / IaC diff summary

* `api/app/jwt_utils.py`:
    * Added `_decode_jwt_payload(authz)` (shared helper).
    * Added `_decode_jwt_oid(authz)` — returns `oid` falling back to
      `sub`, truncated to 128 chars (matches `_decode_jwt_upn`).
* `api/app/middleware.py`:
    * Imported `_decode_jwt_oid` and `redact_oid`.
    * Both the `LOGGER.exception("req_failed ...")` line and the
      `LOGGER.log(level, "req ...")` line now include `caller_hash=%s`
      computed from the bearer's `oid` / `sub` claim via `redact_oid`.
* `api/tests/test_caller_hash_logging.py` — 10 new tests covering:
  anonymous → `caller_hash=None`, authenticated → expected hash, raw
  OID/UPN redaction on both 4xx and 5xx paths, plus 4 direct
  `_decode_jwt_oid` unit tests (oid claim, sub fallback, missing /
  malformed bearer, 128-char truncation).
* **No** Bicep, IaC, or container-image changes. **No** persona-matrix
  changes (no auth surface touched).

## Validation evidence

* Focused: `uv run pytest -q api/tests/test_caller_hash_logging.py` → **10 passed in 2.37s**.
* Wide: `uv run pytest -q api/tests` → **2152 passed, 3 skipped in 33.60s**.
* Lint: `uv run ruff check api/app/jwt_utils.py api/app/middleware.py api/tests/test_caller_hash_logging.py`
  → **All checks passed!** (one auto-fixable import-order nit was fixed by `ruff --fix`).
* Frontend: no `web/src/**` files touched — `npm run build` not required.
* IaC: no Bicep touched — `azd provision --preview` not required.

## Hardening discipline (§12a):

- [x] In scope: observability/audit (additive log token only, no validation gate)
- [x] RBAC change is single-PR safe (no role narrowed) — no RBAC change in this PR
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass — wide sweep green
- [x] Reader allowlist unchanged — no Reader-required route touched
- [x] Capability Probe passes locally — no new Azure surface, probe unaffected
- [x] New guard ships default-OFF — N/A (additive observability per Rule 4 scoping; no `STRICT_*` flag needed)
- [x] No `Depends(require_caller)` added to an SSE event stream — no SSE changes
- [x] Change note (this file) summarises persona impact: every persona's requests now produce a redacted `caller_hash` token; raw OID / UPN never appears in the completion line, validated by the redaction regression tests
