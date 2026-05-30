# 2026-05-30 — terminal WebSocket close-code metrics (audit P3 #29)

## Motivation

Audit P3 #29 noted that the terminal WebSocket proxy in
`api/routes/terminal/ws.py` closes the connection with five distinct
codes depending on what failed (`4401` auth, `4403` origin, `1011`
upstream connect / upstream error, `1000` normal close), but only the
`4403` and `1011` paths emitted a log line. The dashboard's
session-telemetry chart could not tell apart "user closed the tab" from
"ticket expired" from "ttyd sidecar died" — every reconnect storm looked
identical.

The fix introduces `_log_ws_close`, a single structured-audit helper
that emits exactly one `terminal_ws_close code=<int> reason=<str>
session_id=<str> owner_hash=<str> upn_hash=<str> ...` line per close.
Every existing close call site is wrapped, including the normal
end-of-session close that had no log at all before this PR. Identity
fields go through `redact_oid` so the audit stream never leaks raw OID /
UPN — same contract as the rest of the module (charter §11).

This is purely additive observability — no behavioural change — so it
is **not** gated behind a `STRICT_*` env var. §12a Rule 4 explicitly
scopes the default-OFF requirement to "new positive validation"
changes; an extra log line is observability, not validation.

## User-facing change

* Every terminal WebSocket close now produces a `terminal_ws_close`
  audit line that the App Insights / Container Apps log shipper can
  group by `close_code`. The five codes carry these phases:
    * `4401 phase=ticket` — invalid / expired / replayed ticket
    * `4403 phase=origin` — CSWSH origin guard tripped
    * `1011 phase=upstream_connect` (with `error_class`) — ttyd unreachable
    * `1011 phase=proxy` (with `error_class`) — mid-stream upstream error
    * `1000 phase=complete` (with `browser_initiated`,
      `upstream_initiated`) — normal close
* `code=1000` logs at `INFO`; every other code logs at `WARNING` so a
  default `level>=WARNING` filter catches every failure mode.
* No new env var, no new CSP / CORS surface, no new browser behaviour.

## API / IaC diff summary

* `api/routes/terminal/ws.py`:
    * New helpers `_ws_close_severity(code)` and `_log_ws_close(...)`
      that wrap `LOGGER.log(...)` with a stable token format.
    * Each `await websocket.close(...)` in `ws_terminal` now records the
      close via `_log_ws_close` immediately before issuing the close
      frame, so the audit fires even if the close itself raises.
* `api/tests/test_terminal_ws_close_metrics.py` — 10 new tests
  covering: severity routing, the four failure paths with their
  expected `phase` / `error_class` fields, raw OID/UPN redaction, the
  "no identity → `None`" edge case, free-form extra kwargs, the
  severity-fall-through canary, and a regex regression-canary on the
  leading message tokens so any downstream KQL parser stays valid.
* **No** Bicep, IaC, or container-image changes. **No** persona-matrix
  changes (no auth surface touched).

## Validation evidence

* Focused: `uv run pytest -q api/tests/test_terminal_ws_close_metrics.py`
  → **10 passed in 2.40s**.
* Wide: `uv run pytest -q api/tests` → **2142 passed, 3 skipped in 33.19s**.
* Lint: `uv run ruff check api/routes/terminal/ws.py api/tests/test_terminal_ws_close_metrics.py`
  → **All checks passed!** (one auto-fixable import-order nit was fixed by `ruff --fix`).
* Frontend: no `web/src/**` files touched — `npm run build` not required.
* IaC: no Bicep touched — `azd provision --preview` not required.

## Hardening discipline (§12a):

- [x] In scope: observability/audit (additive log lines only, no validation gate)
- [x] RBAC change is single-PR safe (no role narrowed) — no RBAC change in this PR
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass — wide sweep green
- [x] Reader allowlist unchanged — no Reader-required route touched
- [x] Capability Probe passes locally — no new Azure surface, probe unaffected
- [x] New guard ships default-OFF — N/A (additive observability per Rule 4 scoping; no `STRICT_*` flag needed)
- [x] No `Depends(require_caller)` added to an SSE event stream — no SSE changes
- [x] Change note (this file) summarises persona impact: every persona gets identical extra logging; no behavioural difference between owner / contributor / reader / dev_bypass sessions
