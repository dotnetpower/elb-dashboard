# HTTPException detail sanitisation (audit P1 #7 #8)

**Audience:** maintainers, browser/proxy log readers.
**Status:** code change, no API/IaC change. PR-2 of the audit-remediation
series under [§12a Security Hardening Discipline](2026-05-30-security-hardening-governance.md).

## Motivation

The 2026-05-30 audit (items P1 #7 and #8) flagged 11 HTTP routes that returned
raw `str(exc)` text in `HTTPException(detail=…)`. Azure SDK `ValueError` /
`PermissionError` text routinely carries SAS URLs, bearer tokens, account
keys, connection strings, and subscription / object GUIDs — exactly the
substrings the `api.services.sanitise.sanitise` helper exists to strip.

Without the wrap, those tokens propagate through the browser, intermediary
proxies, and any user-side request logger as cleartext.

## User-facing change

None functional. Error responses keep the same status code and field shape;
only the `detail` string is masked + capped to 200 characters. Example:

Before:

```json
{"detail": "Could not fetch https://acc.blob.core.windows.net/c/k?sv=2024-01-01&sr=b&sig=ABCDEFGHIJKLM12345"}
```

After:

```json
{"detail": "Could not fetch https://acc.blob.core.windows.net/c/k?sv=2024-01-01&sr=b&sig=<redacted>"}
```

## API / IaC diff summary

| File | Change |
|------|--------|
| `api/routes/monitor/metrics.py` | Add `sanitise` import. `path_prefix` ValueError → `sanitise(str(exc))[:200]`. |
| `api/routes/warmup.py` | Add `sanitise` import. 2 sites wrapped (`auto_preference_put`, `release`). |
| `api/routes/monitor/aks.py` | 2 sites wrapped (`pod_delete` 403 + 400). |
| `api/routes/aks/autostop.py` | Extend existing `sanitise` import. 1 site wrapped (`upsert` 400). |
| `api/routes/settings/vnet_peering.py` | Extend existing import. 3 sites wrapped (peering 404, dry-run 400, apply 400). |
| `api/routes/blast/databases.py` | Extend existing import. 1 site wrapped (`preview` 400). The pre-existing `[:200]` cap is preserved by `sanitise(...)[:200]`. |
| `api/routes/upgrade.py` | Add `sanitise` import. 2 sites wrapped (upgrade start 409, rollback start 409) — **not in initial audit but caught by the new AST regression scan**. |
| `api/tests/test_httpexception_sanitise.py` | **New regression guard.** AST-based scan that walks every `HTTPException(...)` call in `api/routes/`, classifies `detail` kwarg / second positional arg, and rejects raw `str(exc)` unless wrapped in a sanitising function. Includes a sanity test confirming `sanitise` masks the SAS / Bearer / connection-string / GUID payload shapes that real Azure SDK errors produce. |

## Validation evidence

```
$ uv run ruff check api/routes api/tests/test_httpexception_sanitise.py
All checks passed!

$ uv run pytest -q api/tests/test_httpexception_sanitise.py
65 passed in 2.32s

$ uv run pytest -q api/tests
2072 passed, 3 skipped in 33.09s
```

The wide sweep went from 2007 → 2072 (+65 new regression-guard cases). No
existing test asserted on the exact pre-sanitise detail string — no fixture
or mock had to be updated.

## §12a Hardening discipline checklist

- [x] In scope: **sanitise** (HTTP response sanitisation at the boundary).
- [x] RBAC change is single-PR safe — **no role narrowed**.
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
      (full suite green: 2072 passed).
- [x] Reader allowlist **unchanged**.
- [x] Capability Probe N/A — this PR does not touch RBAC.
- [x] Not a new validation guard — N/A for Rule 4 default-OFF gate. The
      sanitisation is unconditional because the prior `detail=str(exc)`
      sites were a token-leak surface, not a feature.
- [x] No `Depends(require_caller)` added to an SSE event stream — N/A.
- [x] Persona impact: **none** — all personas still see the same error code
      and the same field shape; only the `detail` string is masked. No action
      a Reader / Contributor / Owner can take is gained or lost.

## Follow-up

- **PR-3 (next):** Production guards — force-OFF `TERMINAL_WS_ALLOW_ANY_ORIGIN`
  and `EXEC_HOST` overrides when `CONTAINER_APP_NAME` is set, reject
  `DEV_BYPASS_OID` from `is_upgrade_admin` (audit P0 #4 #5, P1 #11).
- The new AST regression scan is now the standing guard. Any future route
  adding a raw `str(exc)` to an HTTPException will fail
  `test_routes_sanitise_httpexception_detail`.
