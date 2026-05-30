# 2026-05-30 — `STRICT_CORS` narrows methods + headers (audit P2 #12)

## Motivation

Security audit item **P2 #12**: the api sidecar's CORSMiddleware was
configured with `allow_methods=["*"]` and `allow_headers=["*"]`. With
`allow_credentials=True` this is not the OWASP-listed wildcard issue
(those guards already refuse `allow_origins=["*"]`) but it is more
generous than necessary — any method or header the browser asks for
in the preflight is echoed back. The audit asked for an explicit
allowlist that matches what the dashboard SPA actually sends.

## User-facing change

- **Default behaviour unchanged.** Per charter §12a Rule 4 the new
  narrowing is gated behind `STRICT_CORS=true`. When the flag is
  unset, CORSMiddleware is configured with `["*"]` for methods and
  headers exactly as today.
- **When `STRICT_CORS=true`**:
  - Methods narrowed to `GET,POST,PUT,DELETE,OPTIONS` (every method
    the SPA actually sends; PATCH/HEAD/CONNECT/TRACE refused).
  - Headers narrowed to `Authorization,Content-Type,x-client-request-id`
    (every header the SPA actually sends).
  - Operators with custom flows override with
    `STRICT_CORS_ALLOW_METHODS=GET,POST,PATCH,…` and/or
    `STRICT_CORS_ALLOW_HEADERS=Authorization,Content-Type,X-Custom,…`.

## API / IaC diff summary

### `api/main.py`

- The CORSMiddleware block now branches on `STRICT_CORS=true`. The
  legacy `["*"]` path is preserved as the default.
- New env vars (no Bicep change yet — flipping the gate is a separate
  post-soak PR per Rule 4):
  - `STRICT_CORS` — turns on narrowing.
  - `STRICT_CORS_ALLOW_METHODS` — optional comma-separated override.
  - `STRICT_CORS_ALLOW_HEADERS` — optional comma-separated override.

### Tests

- New `api/tests/test_strict_cors.py` (8 tests) covers:
  - OFF: wildcard method expansion and wildcard header echoing.
  - ON: known method (POST) accepted, unknown method (PATCH) rejected.
  - ON: known header (Authorization) accepted, unknown header rejected.
  - ON: method override accepts the extra method.
  - ON: header override accepts the extra header.

## Validation evidence

```
$ uv run pytest -q api/tests/test_strict_cors.py
........  [100%]
8 passed in 4.16s

$ uv run pytest -q api/tests
2117 passed, 3 skipped in 33.49s

$ uv run ruff check api/main.py api/tests/test_strict_cors.py
All checks passed!
```

No new deployment required. Container App env vars unchanged — the
narrowing is dormant until an operator opts in.

## Hardening discipline (§12a)

- [x] In scope: cors
- [x] RBAC change is single-PR safe (no role narrowed) — **N/A**, no RBAC
- [x] Persona Matrix tests pass for owner / contributor / reader /
      dev_bypass — full suite green (2117 passed)
- [x] Reader allowlist unchanged
- [x] Capability Probe passes locally — **N/A for code-only change**
- [x] New guard ships default-OFF behind `STRICT_*` env var —
      `STRICT_CORS` defaults to OFF; both the wildcard echo path and
      the narrowed-allowlist path are covered by tests
- [x] No `Depends(require_caller)` added to an SSE event stream
- [x] Change note (this file) summarises persona impact: every persona
      keeps working unchanged while `STRICT_CORS` is unset (the
      default). Once flipped, only requests that try to use a method
      or header outside the (overridable) allowlist are rejected at
      preflight — the SPA never does that today.
