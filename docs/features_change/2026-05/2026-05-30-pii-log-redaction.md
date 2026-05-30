# PII log redaction for `caller.object_id` (audit P0 #1)

**Audience:** operators, future audit-remediation PR authors.
**Status:** code change, no API/IaC change. First of the ~11 audit-remediation
PRs landing under [§12a Security Hardening Discipline](../2026-05/2026-05-30-security-hardening-governance.md).

## Motivation

The 2026-05-30 permission-risk audit flagged P0 #1: 22 `LOGGER.*` call sites
across 13 route files were emitting `caller.object_id` (the Entra ID GUID of
the signed-in user) in cleartext. These lines flow into Application Insights
and any log aggregator, which:

- exposes a stable PII correlation key,
- makes it trivial to enumerate all activity of a single user, and
- contradicts the charter §12 sanitisation rule.

The matching `_log_identity_hash` helper in
[api/routes/terminal/ws.py](../../../api/routes/terminal/ws.py) already used a
sha256 prefix internally but was not reused anywhere else.

## User-facing change

None. All affected log lines keep the same fields and order; only the value of
the `caller_oid=…` / `oid=…` / `by=…` token changes from a 36-char GUID to a
deterministic 12-char sha256 prefix. Operators can still grep a single user's
trail by the prefix — they just cannot reverse it.

Example before:

```
external BLAST submit accepted caller_oid=11111111-2222-3333-4444-555555555555 db=swissprot program=blastp
```

Example after:

```
external BLAST submit accepted caller_oid=d4e5f60718a9 db=swissprot program=blastp
```

## API / IaC diff summary

| File | Change |
|------|--------|
| `api/services/sanitise.py` | New `redact_oid(value)` — deterministic `hashlib.sha256(value.encode()).hexdigest()[:12]`, returns `None` for falsy input. Single source of truth so every route imports the same helper. |
| `api/routes/terminal/ws.py` | `_log_identity_hash` now delegates to `redact_oid` (back-compat preserved — same signature, same output shape for non-empty input). |
| `api/routes/elastic_blast.py` | 6 `LOGGER.info` sites wrapped. |
| `api/routes/settings/app_insights.py` | 3 sites wrapped. |
| `api/routes/aks/autostop.py` | 3 sites wrapped. The ownership-refusal `LOGGER.warning` block refactored from `f"...{caller.object_id[-8:]}" if caller.object_id else "?"` to `redact_oid(caller.object_id) or "?"` for consistency. |
| `api/routes/aks/openapi.py` | 3 sites wrapped (token update, public-https enable/disable). |
| `api/routes/aks/peering.py` | 1 site wrapped. |
| `api/routes/blast/databases.py` | 2 sites wrapped (shard accept, db-order oracle). |
| `api/routes/blast/submit.py` | 1 site wrapped (canonical external submit). |
| `api/routes/resources.py` | 3 sites wrapped (`ensure_rg`, `ensure_storage`, `ensure_acr`). |
| `api/routes/settings/aks_observability.py` | 2 sites wrapped (enable, disable). |
| `api/routes/settings/vnet_peering.py` | 13 sites wrapped — 9 positional LOGGER args via regex sweep, 2 `_validate_target_ip(target_ip, caller.object_id)` call sites rewritten to pass redacted value (signature unchanged so internal warnings now receive redacted value). |
| `api/routes/storage/local_debug.py` | 1 site wrapped. The pre-existing `caller.object_id[:8]` slice on line 186 is treated as already-redacted by the AST scan. |
| `api/routes/storage/prepare_db.py` | 3 sites wrapped. The cancel metadata f-string at line 1572 uses a local `_cancel_oid` var to stay under the 100-char line limit. |
| `api/tests/test_pii_log_redaction.py` | **New regression guard.** AST-based static scan (no FastAPI boot): for every `*.py` under `api/routes/`, walks `LOGGER.*` calls and recursively classifies each argument. Understands `Subscript` slicing, `JoinedStr` / `FormattedValue` f-strings, `IfExp` ternaries, and a `_REDACTING_FUNCTIONS = frozenset({"redact_oid", "_log_identity_hash"})` allowlist. Also asserts `redact_oid` is deterministic and the output is never a substring of the input GUID. |

**Not touched on purpose** (these are audit/storage row fields or Celery task
kwargs, not log lines):

- `api/routes/aks/autostop.py` line 453 `payload["owner_oid"] = caller.object_id`
- `api/routes/aks/openapi.py` Celery `caller_oid=caller.object_id or ""` kwargs
- `api/routes/blast/databases.py` line 546 `"requested_by": caller.object_id`
- `api/routes/blast/submit.py` task kwargs / audit dict fields
- `api/routes/resources.py` `caller_oid=caller.object_id` kwargs to `monitoring_svc` helpers

These are deliberately raw because they are persisted as audit trail and need
to remain joinable against the platform identity. PR-2 (sanitised exception
detail) and PR-7 (audit table hashing strategy) revisit them separately.

## Validation evidence

```
$ uv run ruff check api/routes/ api/services/sanitise.py api/tests/test_pii_log_redaction.py
All checks passed!

$ uv run pytest -q api/tests/test_pii_log_redaction.py
66 passed in 7.50s

$ uv run pytest -q api/tests
2007 passed, 3 skipped in 53.08s
```

The wide sweep confirms no test asserted on the exact pre-redaction log line
content (no fixture or mock had to change).

## §12a Hardening discipline checklist

- [x] In scope: **sanitise** (output sanitisation at log boundary).
- [x] RBAC change is single-PR safe — **no role narrowed**. N/A for Rule 1
      2-phase rule.
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
      (full suite green: 2007 passed).
- [x] Reader allowlist **unchanged**.
- [x] Capability Probe N/A — this PR does not touch RBAC; the probe is wired
      but not re-asserted.
- [x] Not a new validation guard — N/A for Rule 4 default-OFF gate. The
      redaction is unconditional because the prior log lines were a PII leak,
      not a feature.
- [x] No `Depends(require_caller)` added to an SSE event stream — N/A.
- [x] Change note (this file) summarises persona impact: **none** — all
      personas still see the same log lines, just with a 12-char hash instead
      of the raw GUID. No action a Reader / Contributor / Owner can take is
      gained or lost.

## Follow-up

- **PR-2 (next):** Sanitise `HTTPException(detail=str(exc))` sites in
  `api/routes/monitor/metrics.py` and `api/routes/settings/vnet_peering.py`
  (audit P1 #7, #8).
- **PR-7 (later):** Decide whether the audit-table `owner_oid` / `requested_by`
  fields should be hashed at storage time. Currently they remain raw to keep
  the audit trail joinable against Entra.
