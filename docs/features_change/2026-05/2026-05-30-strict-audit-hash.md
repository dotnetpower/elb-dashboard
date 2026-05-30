# 2026-05-30 — `STRICT_AUDIT_HASH` hashes PII in jobhistory payloads (audit P2 #13 #14)

## Motivation

Security audit items **P2 #13** (PII in append-blob audit history) and
**P2 #14** (PII echoed into App Insights custom dimensions). The
`jobhistory` table stores per-event `payload_json` blobs that today
include `caller_oid`, `owner_oid`, `upn`, `actor_oid`, and friends in
clear text. Any forensic export of the table (or any App Insights
query against the same payload shape) therefore leaks the operator's
Entra identity. The audit asked for a hashed-at-write posture so the
on-disk row carries only a deterministic fingerprint of the OID/UPN.

## User-facing change

- **Default behaviour unchanged.** Per charter §12a Rule 4 the new
  hashing is gated behind `STRICT_AUDIT_HASH=true`. When the flag is
  unset, `append_history` writes the payload verbatim, exactly as
  today.
- **When `STRICT_AUDIT_HASH=true`**, every PII-bearing key inside the
  payload dict is replaced with `redact_oid(value)` (sha256[:12]) at
  write time. The audit list response then shows the same fingerprint
  the redacted server logs do, so an operator can correlate "this
  audit row was triggered by the same caller as that log line"
  without ever recovering the raw OID.

### PII key detection

The matcher (in `_redact_audit_payload`) recognises:

- **Exact** key matches: `oid`, `upn`, `email`, `actor`, `principal`,
  `principal_id`, `object_id`, `preferred_username`, `user_id`,
  `userid`.
- **Suffix** matches: `_oid`, `_upn`, `_email`, `_actor` (so
  `caller_oid`, `owner_oid`, `parent_oid`, `actor_oid`, etc. all
  qualify).
- Walks nested dicts and lists; leaves scalars and non-PII keys
  untouched. Lookalike keys (`void`, `paranoid`, `cosmosdb_resource_id`)
  are explicitly preserved by the regression tests.
- Empty / `None` PII values are NOT hashed (hashing `""` is meaningless
  and would just add noise to forensic exports).

## API / IaC diff summary

### `api/services/state/repository.py`

- New module-level `_PII_KEY_EXACT`, `_PII_KEY_SUFFIXES`, `_is_pii_key`.
- New `_redact_audit_payload` deep-walker that uses the existing
  `api.services.sanitise.redact_oid` for the actual hashing.
- `append_history` branches on `STRICT_AUDIT_HASH=true` and walks the
  payload before `json.dumps` when the flag is set.
- No Bicep change — gate is dormant until a separate post-soak PR
  flips the Container App env.

### Tests

- New `api/tests/test_strict_audit_hash.py` (8 tests) covers:
  - Helper redacts every known PII key shape (caller_oid, owner_oid,
    upn, actor_oid in nested dict, email in nested list, principal_id
    by exact match).
  - Helper is deterministic (two passes produce the same hash).
  - Helper preserves safe fields and lookalike keys.
  - Helper handles empty / None PII values without re-hashing them.
  - Helper passes through non-dict input untouched.
  - `append_history` OFF path writes verbatim payload.
  - `append_history` ON path persists hashed payload, non-PII
    untouched.
  - `append_history` ON path tolerates `payload=None`.

## Validation evidence

```
$ uv run pytest -q api/tests/test_strict_audit_hash.py
........  [100%]
8 passed in 2.27s

$ uv run pytest -q api/tests
2125 passed, 3 skipped in 33.72s

$ uv run ruff check api/services/state/repository.py api/tests/test_strict_audit_hash.py
All checks passed!
```

No new deployment required. Historical `jobhistory` rows are
untouched — the gate only affects writes from this point forward, and
even then only when an operator explicitly turns it on.

## Hardening discipline (§12a)

- [x] In scope: sanitise (audit-log writer)
- [x] RBAC change is single-PR safe — **N/A**, no RBAC change
- [x] Persona Matrix tests pass for owner / contributor / reader /
      dev_bypass — full suite green (2125 passed)
- [x] Reader allowlist unchanged — **N/A**, no role surface
- [x] Capability Probe passes locally — **N/A for code-only change**
- [x] New guard ships default-OFF behind `STRICT_*` env var —
      `STRICT_AUDIT_HASH` defaults to OFF; both the verbatim path and
      the hashed path are covered by integration tests
- [x] No `Depends(require_caller)` added to an SSE event stream
- [x] Change note (this file) summarises persona impact: no persona
      sees a behavioural change while `STRICT_AUDIT_HASH` is unset.
      Once flipped, audit-log responses show 12-char OID/UPN
      fingerprints instead of raw GUIDs — the same fingerprint the
      sanitised server logs already use, so an operator's existing
      correlation workflow gains the audit row, it doesn't lose it.
