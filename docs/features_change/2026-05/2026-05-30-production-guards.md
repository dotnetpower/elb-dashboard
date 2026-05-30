# Fail-closed production guards (audit P0 #4 #5, P1 #11)

## Motivation

The May 2026 security audit flagged three escape hatches that, while harmless in
local dev, become unauthenticated-shell or privilege-escalation paths the moment
the same image runs inside the deployed `ca-elb-dashboard` Container App. They
all share the same shape: a dev-convenience knob that is honoured everywhere,
including production, because the code had no production guard.

| Audit | Surface | Old behaviour | Risk in production |
| --- | --- | --- | --- |
| P0 #4 | `api/routes/terminal/ws.py` — `TERMINAL_WS_ALLOW_ANY_ORIGIN=true` | Skipped the WebSocket Origin allowlist for *any* connection that presented a valid ticket | A leaked or guessed ticket from any origin (including cross-site) would open `/api/terminal/ws` |
| P0 #5 | `terminal/exec_server.py` — `EXEC_HOST` env var | Bound the loopback exec server to whatever value `EXEC_HOST` carried (default `127.0.0.1`) | Setting `EXEC_HOST=0.0.0.0` in a Container App revision exposed the exec server to the VNet and any sidecar that could reach it |
| P1 #11 | `api/services/upgrade/auth.py` — `is_upgrade_admin()` | Recognised the synthetic `DEV_BYPASS_OID` as upgrade admin whenever `UPGRADE_ADMIN_OIDS` listed it, regardless of `AUTH_DEV_BYPASS` actually being on | A stale `UPGRADE_ADMIN_OIDS=00000000-…` in a deployed env let any caller hitting the dev-bypass path obtain Upgrade Admin |

Closing these is *not* a §12a Rule 4 default-OFF gate — they are not new features,
they are existing security bugs whose only fix is to deny the bad behaviour in
production. Local dev is unaffected because the guard is keyed on the
`CONTAINER_APP_NAME` env variable that only the ACA platform sets.

## User-facing change

* **Browser terminal** — opening `/api/terminal/ws` in a deployed Container App
  always enforces the Origin allowlist. `TERMINAL_WS_ALLOW_ANY_ORIGIN=true` is
  silently ignored in deployed environments and continues to work locally.
* **Terminal sidecar** — `exec_server.py` refuses to start with a
  `RuntimeError("exec_server refuses to start with EXEC_HOST=… inside a
  Container Apps revision; pin to 127.0.0.1")` if `EXEC_HOST` is set to anything
  other than `127.0.0.1`, `localhost`, or `::1` while `CONTAINER_APP_NAME` is
  set. The misconfiguration becomes visible at sidecar start, not at first
  request.
* **Upgrade routes** — calls authenticated by the dev-bypass identity
  (`AUTH_DEV_BYPASS=true`) are rejected with 403 from every `is_upgrade_admin()`
  gate in a deployed Container App, regardless of `UPGRADE_ADMIN_OIDS`. Real
  callers carrying the `UpgradeAdmin` role claim or a non-bypass OID in the
  allowlist are unaffected.

## API / IaC diff summary

```
api/routes/terminal/ws.py         |   7 ++  (AND CONTAINER_APP_NAME unset into _TERMINAL_WS_ALLOW_ANY_ORIGIN)
api/services/upgrade/auth.py      |  16 ++  (import DEV_BYPASS_OID, top-of-body production guard in is_upgrade_admin)
terminal/exec_server.py           |  20 ++  (hard-fail at import time when non-loopback bind requested in ACA)
api/tests/test_persona_matrix.py  | 149 ++  (Section 5: 9 new tests covering the three guards both ON and OFF)
api/tests/test_upgrade_routes.py  |  51 +/-  (fixture clears CONTAINER_APP_NAME; escape-hatch test uses dependency override for admin)
```

No Bicep changes. No new sidecar. No new env var. No deploy required.

## Validation evidence

```
$ uv run pytest -q api/tests/test_persona_matrix.py
41 passed in 3.05s

$ uv run pytest -q api/tests/test_upgrade_routes.py
30 passed in 14.51s

$ uv run pytest -q api/tests
2081 passed, 3 skipped in 33.54s

$ uv run ruff check api terminal
All checks passed!
```

The wide-sweep test count moves from 2072 (PR-2 baseline) → 2081 (this PR), exactly
matching the 9 new persona-matrix tests added in Section 5.

## §12a charter checklist

```
Hardening discipline (§12a):
- [x] In scope: auth | network | sanitise
- [x] RBAC change is single-PR safe (no role narrowed)
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass (41/41 in 3.05s)
- [x] Reader allowlist unchanged
- [x] Capability Probe unchanged (no new role required)
- [x] N/A — these guards CLOSE existing escape hatches that were security bugs,
      not new features; §12a Rule 4 default-OFF gate does not apply
- [x] No `Depends(require_caller)` added to an SSE event stream
- [x] Change note under `docs/features_change/2026-05/` summarises persona impact (this file)
```

## Persona impact

* **Owner / Contributor / Reader** — no change. None of these personas relied on
  the bypass paths being honoured in production.
* **dev_bypass (`AUTH_DEV_BYPASS=true`, OID `00000…0`)** — loses upgrade-admin
  access in deployed Container Apps. Local dev path
  (`CONTAINER_APP_NAME` unset) is unchanged. This is the intended fix for audit
  P1 #11; operators who legitimately want admin in production must add their
  real OID to `UPGRADE_ADMIN_OIDS` or grant the `UpgradeAdmin` app role.

## Not deployed

Per charter §13 "Do NOT redeploy for ordinary code changes", PR-3 ships as a
code change only. The next regular release will roll the guards out via the
existing image rebuild. `EXEC_HOST` and `TERMINAL_WS_ALLOW_ANY_ORIGIN` are not
set by the deployed Container App template today, so the production-blocking
branches of the new code are dormant until somebody tries to set them.
