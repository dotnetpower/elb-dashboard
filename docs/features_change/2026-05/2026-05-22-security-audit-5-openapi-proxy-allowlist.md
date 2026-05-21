# Security audit 2026-05-22 — item #5

## Motivation
[`/api/aks/openapi/proxy`](../../../api/routes/aks/openapi.py) accepts an
arbitrary `target_path` and forwards the request to the deployed
`elb-openapi` service while auto-injecting the admin `X-ELB-API-Token`.
The original validation only rejected paths that did not start with `/`
or that contained CR / LF, plus a narrow check for dashboard-style UUID
job paths.

That left a **privilege escalation surface**: any authenticated tenant
member could pass `?path=/admin/...`, `?path=/internal/...`, etc. and
ride the auto-injected admin token straight into admin-only endpoints
on the OpenAPI service.

Finding ID: #5 (HIGH) from the 2026-05-22 read-only security sweep.

## User-facing change
- `GET|POST|PUT|PATCH|DELETE /api/aks/openapi/proxy?path=…` now returns
  `400 {"code": "openapi_path_not_allowlisted", "message": "..."}` when
  the path does not start with one of the four allowlisted prefixes used
  by the SPA's API Reference "Try it" feature:
  - `/healthz`
  - `/openapi.json`
  - `/docs/`
  - `/v1/`
- Returns `400 {"code": "openapi_path_traversal_denied"}` when the path
  contains `..` after iterative percent-decoding (covers literal `..`,
  `%2e%2e`, double-encoded `%252e%252e`).
- Returns `400 {"code": "openapi_path_not_allowlisted", "message": "...denied segment..."}`
  when the path or its query string contains `/admin/`, `/admin?`,
  `/internal/`, `/internal?`, `/debug/`, `/debug?` — defence-in-depth
  against an upstream that exposes admin routes inside an allowlisted
  prefix (e.g. `/v1/admin/...`).
- Returns `400 {"code": "invalid_openapi_path", "message": "...control characters"}`
  when the decoded path contains any C0 control byte (including NUL),
  which can otherwise truncate the path at the upstream router.
- Comparison is **case-insensitive** so an ingress that lower-cases the
  path cannot launder `/Admin/foo` through the gate.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Routes | [api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py) | New `_enforce_openapi_proxy_target_path` helper + `_OPENAPI_PROXY_ALLOWED_PATH_PREFIXES` / `_OPENAPI_PROXY_DENIED_PATH_TOKENS` tuples. Helper is called after the existing `\r`/`\n`/`//` checks and before `_reject_dashboard_uuid_job_path`. |
| Tests | [api/tests/test_openapi_proxy_route.py](../../../api/tests/test_openapi_proxy_route.py) | 11 new regression tests: 6 base (admin denied, internal denied, literal `..` denied, URL-encoded `..` denied, `/healthz` still works, `/openapi.json` + `/docs/...` still work) + 5 hardening (case-insensitive `/Admin`, `/v1/admin` deny token, `/v1/admin?` in query, NUL byte, `/docs.json` prefix-extension). |

No IaC changes. No new dependencies. No deploy required.

## Validation evidence
- `uv run ruff check api/routes/aks/openapi.py api/tests/test_openapi_proxy_route.py` → passed.
- `uv run pytest -q api/tests/test_openapi_proxy_route.py` — **19 passed**.
- `uv run pytest -q api/tests` — **901 passed** (was 883 → +18 from #5 + hardening).

## Hardening pass (same day)
A self-critique surfaced four additional weaknesses; fixed in the same
change:

- **CRITICAL — Case-sensitivity bypass.** The first draft compared the
  path against lowercase allowlist entries with `str.startswith`. An
  ingress that lower-cases the path (some nginx configs, AKS ingress
  rewrite rules) could turn `/Admin/foo` into `/admin/foo` *after* the
  api had already accepted it. Fixed: `path_only.lower()` before
  comparison.
- **HIGH — Allowed prefix laundering admin routes.** `/v1/` is broad. If
  the elb-openapi service ever exposes `/v1/admin/...` or similar, the
  first draft would let it through. Fixed: explicit deny-token list
  (`/admin/`, `/admin?`, `/internal/`, `/internal?`, `/debug/`,
  `/debug?`) checked against `path + query string`.
- **MEDIUM — NUL byte truncation.** A path like `/v1/safe\x00/admin`
  would pass the allowlist (because the api saw `/v1/safe` after
  truncation in some checks) while the upstream router saw something
  different. Fixed: reject any C0 control byte (`ord < 0x20`) after
  percent-decode.
- **LOW — Trailing-slash prefix laundering.** `/docs` (no trailing
  slash) would have matched `/docs.json` or `/docsBYPASS` as a prefix.
  Fixed: allowlist entries that should be prefix-only end in `/`
  (`/docs/`, `/v1/`); the helper accepts the exact root path
  (`prefix_root = prefix.rstrip("/")`) but rejects extensions like
  `/docs.json`.

New regression tests cover each of these vectors so a future refactor
that loses the property breaks the test, not production.

## Non-goals (deferred)
- Tightening the `/v1/` prefix to a per-endpoint allowlist (e.g.
  `/v1/jobs`, `/v1/databases` only) would break the SPA's dynamic
  Try-It surface as soon as elb-openapi adds a new public endpoint.
  Defer until role-based authz (#1/#4) lets us distinguish
  Operator / Admin callers and grant the broader Try-It surface to
  Operators only.
