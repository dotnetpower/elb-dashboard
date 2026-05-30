# 2026-05-30 — STRICT_EXEC_RATE_LIMIT gate on the loopback exec server

## Motivation

Audit P2 #22 and P2 #28 flagged that the existing concurrency cap on
`terminal/exec_server.py` (a `BoundedSemaphore(4)`) only restricts
**concurrent** calls — a fast-finishing caller (think: a Celery task that
loops `kubectl get pods` every 10 ms) can sustain thousands of requests
per minute without ever blocking on the semaphore, because each call
releases its slot the moment the subprocess exits. That blew past the
sidecar's intended throughput budget and made every misconfigured
backoff loop a one-step DoS against the terminal sidecar.

The fix adds a second control plane — a per-binary sliding-window rate
limiter — that counts completed attempts per minute and returns HTTP 429
with `Retry-After` once the cap is exhausted, so the api caller backs off
cleanly instead of looping.

Per §12a Rule 4 the new guard ships **default-OFF** behind
`STRICT_EXEC_RATE_LIMIT=true`; the deployed posture is unchanged until a
later PR flips the default ON after a soak cycle. The change is
back-compat to the byte (no existing test had to change).

## User-facing change

* When `STRICT_EXEC_RATE_LIMIT=true` is set on the `terminal` sidecar:
    * Each allowed binary (`azcopy`, `kubectl`, `elastic-blast`, `elb`,
      `az`, `git`) gets its own sliding-window bucket — a hot binary
      cannot starve another binary's quota.
    * Defaults: 120 accepts per 60 s window (`EXEC_RATE_LIMIT_PER_WINDOW`
      / `EXEC_RATE_LIMIT_WINDOW_SECONDS`).
    * Exceeding the cap returns `HTTP 429` with `Retry-After: <seconds>`
      and a JSON body `{error, binary, retry_after_seconds, window_seconds,
      per_window}`.
    * Each rejection is recorded as a single `exec_rate_limited` audit
      line (binary + retry_after — no argv beyond argv[0], same redaction
      contract as the rest of the audit).
* When the env var is unset / `false` the server behaviour is byte-for-byte
  identical to before this PR (matched by the OFF-path test).

## API / IaC diff summary

* `terminal/exec_server.py`:
    * Added `_rate_limit_enabled`, `_rate_limit_window_seconds`,
      `_rate_limit_per_window`, `_rate_limit_check`,
      `_rate_limit_reset_for_tests` and a module-level
      `_RATE_LIMIT_LOCK` + `_RATE_LIMIT_BUCKETS` dict.
    * `do_POST` now calls `_rate_limit_check` immediately after
      `_semaphore.acquire`; on deny it releases the semaphore, emits
      `429 Retry-After`, and audits `exec_rate_limited`.
* `api/tests/test_exec_rate_limit.py` — 7 new tests (6 unit-level, 1
  HTTP-level boot of `ThreadingHTTPServer(module._Handler)` confirming
  the wire-level 429 + `Retry-After`).
* **No** Bicep, IaC, or container-image changes. **No** persona-matrix
  changes (the gate is default-OFF; persona matrix sees the legacy
  behaviour).

## Validation evidence

* Focused: `uv run pytest -q api/tests/test_exec_rate_limit.py` → **7 passed in 3.31s**.
* Wide: `uv run pytest -q api/tests` → **2132 passed, 3 skipped in 32.45s**
  (the +7 are the new file; the 3 skips are the pre-existing
  `test_web_blast_parity_xml.py` skips that require `ELB_PARITY_CANDIDATE_DIR`).
* Lint: `uv run ruff check terminal/exec_server.py api/tests/test_exec_rate_limit.py`
  → **All checks passed!**.
* Frontend: no `web/src/**` files touched — `npm run build` not required.
* IaC: no Bicep touched — `azd provision --preview` not required.

## Hardening discipline (§12a):

- [x] In scope: rate-limit (new guard on a network-adjacent surface, terminal sidecar)
- [x] RBAC change is single-PR safe (no role narrowed) — no RBAC change in this PR
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass — wide-sweep green; gate is default-OFF so matrix sees unchanged behaviour
- [x] Reader allowlist unchanged — no Reader-required route touched
- [x] Capability Probe passes locally — no new Azure surface, probe unaffected
- [x] New guard ships default-OFF behind `STRICT_EXEC_RATE_LIMIT` env var (Rule 4 conformance: both ON-path and OFF-path tests present, including a runtime-flip test that guards against the regression where the flag is read only at import)
- [x] No `Depends(require_caller)` added to an SSE event stream — no SSE changes
- [x] Change note (this file) summarises persona impact: dev-bypass / reader / contributor / owner all unaffected when the env var is unset; when ON, every persona gets the same per-binary backpressure (the limiter does not authenticate, it shapes traffic uniformly per the sidecar's calling MI)
