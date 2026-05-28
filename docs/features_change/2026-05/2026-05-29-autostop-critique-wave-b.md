# 2026-05-29 — Autostop / NCBI / OpenAPI critique fixes wave B (#16 / #18 / #20.5 / #20.6 / #20.12)

## Motivation

Continuation of [wave A](2026-05-29-autostop-critique-wave-a.md) — five more
items from the same critique round, plus one structural unification so
two sources of upstream-code truth cannot drift again.

## User-facing change

- **Auto-stop status now shares its cache across uvicorn workers (#18).**
  The api sidecar runs 2 uvicorn workers per replica; a browser polling
  `/api/aks/autostop/status` at 60 s round-robins between them. Before
  this change each worker recomputed the entire Table+ARM+evaluator
  pipeline on every cache miss because the in-process L1 cache was
  invisible to its sibling. Now an L2 Redis cache (`autostop:status:*`,
  TTL=5 s) sits behind the L1 so a sibling worker serves the cached
  body on its very next poll. Redis unreachable degrades gracefully to
  L1-only — no regression for local-dev without a broker.
- **PLS transition banner picks up the active theme automatically (#20.6).**
  The banner used to hard-code `rgba(255, 196, 0, 0.5)` / `…0.08` so a
  light-mode or high-contrast theme rotation would leave the banner
  stuck on its hard amber. It now derives both border and fill from
  the `--warning` theme token via `color-mix(in srgb, var(--warning) X%, transparent)`.
- **vnet-peering audit `interrupted` write surfaces in the log when it fails (#16).**
  Previously a complete audit-backend outage during an apply could
  leave an Audit row stuck in `started` forever with zero log
  breadcrumb. The interrupted-write now logs `ERROR` with the audit
  job id so an operator chasing the phantom row can grep the api log.

## Internal hardening (no SPA visible change)

- **`OPENAPI_NESTED_UPSTREAM_CODES` is now derived, not hand-rolled (#20.5).**
  The set is computed from `OPENAPI_UPSTREAM_ACTIONS.keys()` minus the
  explicit top-level wrapper set (`{"openapi_unreachable"}`). Adding a
  new sibling `/v1/ready` code now requires touching one place — the
  actions table — instead of two parallel lists.
- **External BLAST in-flight wait has an explicit, env-tunable retry cap (#20.12).**
  The previous "wait → re-check → try-become-leader → wait → re-check →
  fall through" pattern was hand-counted at 2 attempts. It is now a
  bounded loop driven by `_READY_INFLIGHT_MAX_WAIT_ROUNDS` (env
  `OPENAPI_READY_INFLIGHT_MAX_WAIT_ROUNDS`, default 2). Total worst-case
  wait = `rounds × _READY_INFLIGHT_WAIT_SECONDS`, so a pathological
  leader-swap loop cannot pin a single caller for an unbounded duration.

## API / IaC diff summary

### Backend (`api/`)

| File | Change |
|---|---|
| [api/routes/aks/autostop.py](../../../api/routes/aks/autostop.py) | Two-tier cache (`_STATUS_L1_TTL_SECONDS=2` in-process, `_STATUS_L2_TTL_SECONDS=5` Redis); `_status_redis_get/set/delete` helpers; PUT/extend now drop L2 too (#18) |
| [api/routes/settings/vnet_peering.py](../../../api/routes/settings/vnet_peering.py) | `_audit_session` captures `interrupted` bool return and logs ERROR with audit_job id on failure (#16) |
| [api/services/blast/submit_gates.py](../../../api/services/blast/submit_gates.py) | `OPENAPI_NESTED_UPSTREAM_CODES` derived from `OPENAPI_UPSTREAM_ACTIONS - _OPENAPI_TOP_LEVEL_CODES` (#20.5) |
| [api/services/external_blast.py](../../../api/services/external_blast.py) | `_READY_INFLIGHT_MAX_WAIT_ROUNDS` constant + bounded loop replaces the hand-counted 2-attempt pattern (#20.12) |

### Frontend (`web/`)

| File | Change |
|---|---|
| [web/src/pages/apiReference/PlsTransitionBanner.tsx](../../../web/src/pages/apiReference/PlsTransitionBanner.tsx) | Exported `PLS_BANNER_BORDER_COLOR` / `PLS_BANNER_BACKGROUND_COLOR` using `color-mix(in srgb, var(--warning) X%, transparent)` (#20.6) |
| [web/src/pages/apiReference/PlsTransitionBanner.test.ts](../../../web/src/pages/apiReference/PlsTransitionBanner.test.ts) | New — 3 tests pinning the colour pipeline (#20.6) |

### Tests

| File | Change |
|---|---|
| [api/tests/test_aks_autostop_route.py](../../../api/tests/test_aks_autostop_route.py) | New `_FakeRedis` stub + 3 L2 tests: writes-to-L2, serves-from-L2-without-eval, PUT-drops-L2 (#18) |

### IaC

No infra changes in this wave.

## Validation evidence

```text
$ uv run pytest -q api/tests
............................................................... [100%]
1886 passed, 3 skipped in 36.17s

$ cd web && npm test -- --run
 Test Files  55 passed (55)
      Tests  428 passed (428)
   Duration  4.03s

$ uv run ruff check api
All checks passed!
```

## Self-review

- Consumer search for `_STATUS_TTL_SECONDS` (now an alias) confirmed no
  test outside this module compares the value; the alias is kept for
  one-cycle backward compat with anything that imports the name.
- `_OPENAPI_TOP_LEVEL_CODES` documented inline — any future addition
  there narrows the nested set without touching tests.
- `_status_redis_client` is wrapped in try/except at every call site so
  Redis outage degrades to L1-only without raising.
- `_record_audit_event` already swallows all backend errors and returns
  `False`; the new ERROR log path is reachable in unit tests by
  monkeypatching the helper to return `False`.
