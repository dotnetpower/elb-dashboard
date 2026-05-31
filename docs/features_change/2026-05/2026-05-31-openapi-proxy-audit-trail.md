# OpenAPI proxy: forensic audit trail for state-changing "Try it" calls

## Motivation

The `/api/aks/openapi/proxy` route (the SPA `/docs` "Try it" executor) auto-injects
the admin `X-ELB-API-Token` and forwards browser calls to the deployed `elb-openapi`
pod. The dashboard auth layer (`require_caller`) validates single-tenant MSAL bearer
tokens (audience, issuer, `tid`, `oid`, optional `STRICT_JWT` `azp`/`appid`) but does
**not** — and, because OBO flows are forbidden by charter §12, **cannot** — enforce a
per-caller Azure RBAC gate. All Azure work runs under the shared managed identity, so
any authenticated tenant member (including a subscription **Reader**) can drive
state-changing calls — e.g. `POST /v1/jobs` submits a BLAST workload — through the admin
token.

This is an accepted architectural property (gated by tenant membership + the managed
identity's own RBAC, not per-caller RBAC). The remaining gap was **traceability**: there
was no record of *which* dashboard caller drove a privileged mutating call through the
admin-token proxy.

## User-facing change

State-changing OpenAPI proxy calls (`POST` / `PUT` / `PATCH` / `DELETE`) now append a
best-effort forensic audit row before forwarding. The row is owned by the caller's
object id, so it surfaces on the existing `/api/audit/log` SPA panel for the user who
made the call. Read-only `GET` calls are intentionally **not** audited (dashboard
polling noise). No auth decision changes — the action is still allowed for any
authenticated tenant member; it is now merely traceable.

Behaviour is otherwise unchanged: no new gate, no persona impact, no token ever stored
in the audit row.

## API / IaC diff summary

- New `api/services/openapi/proxy_audit.py`:
  - `is_state_changing_method(method)` — classifies the four mutating verbs.
  - `record_openapi_proxy_exec(...)` — appends a token-free `JobState` audit row
    (`type="openapi_proxy_exec"`, `job_id="openapi-proxy:<METHOD>:<cluster>:<ulid>"`),
    mirroring `_record_self_heal_audit`. Best-effort: swallows all repo errors and
    never blocks the proxy. Caps the recorded `target_path` at 512 chars.
- `api/routes/aks/openapi.py` `aks_openapi_proxy`: after token injection and before
  forwarding, for state-changing methods only, calls
  `await asyncio.to_thread(record_openapi_proxy_exec, ...)` so the synchronous Table
  write stays off the event loop.
- No IaC change. No new env var / gate (the change is purely additive logging, so it
  does not need the charter §12a Rule 4 `STRICT_*` default-OFF treatment).

## Persona impact (charter §12a)

- In scope: `audit` (additive logging only — no auth/rbac/network/jwt/ticket/cors change).
- No RBAC role added or narrowed → single-PR safe, no 2-phase split.
- Persona Matrix (`test_persona_matrix.py`) unchanged and green for owner / contributor
  / reader / dev_bypass — no route gating changed.
- Reader allowlist unchanged.
- No `Depends(require_caller)` added to any SSE stream.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_proxy_audit.py` → 13 passed (new helper:
  verb classification, token-free row, `owner_oid="system"` fallback, path-length cap,
  repo-error swallow).
- `uv run pytest -q api/tests/test_openapi_proxy_route.py api/tests/test_openapi_rate_limit.py api/tests/test_route_contracts.py`
  → 37 passed (POST/PUT/PATCH/DELETE proxy behaviour unaffected).
- `uv run pytest -q api/tests/test_openapi_token.py api/tests/test_persona_matrix.py`
  → 49 passed.
- `uv run ruff check api/services/openapi/proxy_audit.py api/routes/aks/openapi.py api/tests/test_openapi_proxy_audit.py`
  → All checks passed.
