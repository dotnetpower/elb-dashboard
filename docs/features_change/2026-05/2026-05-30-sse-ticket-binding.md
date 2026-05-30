# SSE ticket binding hardening (audit P0 #2 #3)

## Motivation

Audit P0 #2 and #3 flagged that the SSE ticket flow used by
`/api/monitor/sidecars/events` and `/api/monitor/logs/{container}/events`
authenticates only by the random 24-byte token. If that token is ever leaked
(via browser extension, accidental log, or a misbehaving proxy), nothing in
the consume path checks where the redemption came from — a different browser,
a different network, or even a different origin could redeem it.

EventSource cannot send `Authorization` headers, so per charter §12a Rule 5 the
fix MUST keep ticket-based auth (not `Depends(require_caller)`) and instead
strengthen the ticket itself. The audit's four recommended tightenings are:

1. Issue endpoint stays `require_caller`-protected. ✅ already in place.
2. Ticket payload binds to caller IP and User-Agent. **New in this PR.**
3. Ticket is one-shot. ✅ already in place (consume pops from the dict).
4. Ticket TTL ≤ 30 s + Origin check on issue endpoint. ✅ TTL was 30 s; **Origin check is new.**

## User-facing change

Two new defences ship behind a single feature flag,
`STRICT_SSE_TICKET_BINDING=true` (default OFF per charter §12a Rule 4):

* **Origin allowlist on issue.** `/sidecars/ticket` and `/logs/ticket` reject
  foreign Origins with 403 when strict mode is on. The allowlist reuses
  `TERMINAL_WS_ALLOWED_ORIGINS` (one knob for SSE + WebSocket).
* **IP + User-Agent binding on consume.** The issue endpoint captures
  `sha256(X-Forwarded-For first hop or client.host)[:16]` and
  `sha256(User-Agent or 'unknown')[:16]` on the ticket. The consume endpoint
  recomputes both hashes from the incoming request and treats any mismatch
  identically to an expired ticket — returns HTTP 204 from the SSE route so
  the browser's native EventSource stops auto-reconnecting and the frontend's
  bounded retry takes over with a fresh ticket.

The flag is read at call time so flipping it does not require a sidecar restart.
Default OFF preserves every existing flow today; flipping to ON is a separate
PR after the soak window per §12a Rule 4.

## API / IaC diff summary

```
api/services/sse_ticket.py        | +146  (new module — is_strict, client_ip_hash, user_agent_hash, origin_allowed, enforce_issue_origin, binding_matches)
api/routes/monitor/sidecars.py    |  ±    (_SidecarTicket gains ip_hash + ua_hash; sidecars_ticket + _consume_sidecar_ticket take Request)
api/routes/monitor/logs.py        |  ±    (_LogTicket gains ip_hash + ua_hash; logs_ticket + _consume_log_ticket take Request)
api/tests/test_sse_ticket_binding.py | +254 (18 tests covering both ON and OFF paths plus helper unit tests)
```

No Bicep changes. No new sidecar. No deploy required.

## Validation evidence

```
$ uv run pytest -q api/tests/test_sse_ticket_binding.py
18 passed in 4.02s

$ uv run pytest -q api/tests/test_sidecars_events_route.py api/tests/test_sidecar_logs.py
14 passed in 3.91s  # legacy OFF-path tests still green

$ uv run pytest -q api/tests
2099 passed, 3 skipped in 33.73s  # 2081 baseline + 18 new = 2099

$ uv run ruff check api
All checks passed!
```

Wide-sweep delta: +18 tests, all green. No existing test had to change.

## §12a charter checklist

```
Hardening discipline (§12a):
- [x] In scope: auth | network | ticket | cors
- [x] RBAC change is single-PR safe (no role narrowed)
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
- [x] Reader allowlist unchanged
- [x] Capability Probe unchanged (no new role required)
- [x] New guard ships default-OFF behind `STRICT_SSE_TICKET_BINDING` env var,
      both ON and OFF path tests included (`test_sse_ticket_binding.py`)
- [x] No `Depends(require_caller)` added to an SSE event stream
      (the consume endpoints stay ticket-based per §12a Rule 5)
- [x] Change note under `docs/features_change/2026-05/` summarises persona impact
```

## Persona impact

When `STRICT_SSE_TICKET_BINDING=true` is eventually flipped on:

* **Owner / Contributor / Reader** — no change for normal browser use. The
  browser SPA always issues + consumes from the same tab → same IP, same UA,
  same Origin. Each EventSource reconnect already re-issues a fresh ticket
  via `/ticket`, so binding holds across reconnects.
* **dev_bypass** — same as above; identity layer is unaffected.
* **CLI / test clients** sharing a token across hosts will fail with 204 (and
  the test runner will see the binding-mismatch as an invalid ticket). The
  test suite uses `TestClient` which is per-test, so this is a no-op.

Default OFF means **zero persona impact in this PR**; flipping is gated behind
the soak window per §12a Rule 4.

## Not deployed

Per charter §13 "Do NOT redeploy for ordinary code changes", PR-4 ships as a
code change only. The flag will be flipped in a separate PR after the soak.
