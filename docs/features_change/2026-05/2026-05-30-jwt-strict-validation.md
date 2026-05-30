# 2026-05-30 — Strict JWT validation (`azp`/`appid` + shorter cache TTL)

## Motivation

Security audit items **P1 #6** and **P1 #9**:

- **#6 (`azp`/`appid` enforcement)** — the bearer-token validator only
  checked `aud`, `iss`, `tid`, `exp`, `iat`, and `oid`. Any other app
  registration inside the same tenant that minted a token for our API
  audience (rare but possible — e.g. a misconfigured downstream app
  with admin consent for our API) would be accepted as long as it had
  the right `aud`. The audit asked for pinning the token to the
  authorised party.
- **#9 (claims cache TTL)** — `_CLAIMS_CACHE_MAX_TTL_SECONDS` was 300 s
  (5 min). If a SPA's admin consent is revoked, the previous decision
  to grant access could be cached for that full window. Audit asked
  for a tighter cap (≤ 60 s) in the hardened profile.

## User-facing change

- **Default behaviour unchanged.** Per charter §12a Rule 4 the new
  guards are gated behind `STRICT_JWT=true`. When the flag is unset,
  validation logic and cache TTL behaviour are byte-for-byte identical
  to today.
- **When `STRICT_JWT=true`**:
  - Tokens must carry an `azp` (AAD v2) or `appid` (AAD v1) claim
    whose value is in `JWT_ALLOWED_APPIDS`. The allowlist defaults to
    the configured `API_CLIENT_ID`, so single-app deployments do not
    need to set `JWT_ALLOWED_APPIDS` explicitly. Operators with
    separate SPA + API app registrations set
    `JWT_ALLOWED_APPIDS=<spa-id>[,<other-id>]`.
  - The claims cache TTL is capped at **60 s** (down from 300 s). The
    25 s polling cycle the dashboard uses is unaffected — the typical
    request still gets a cache hit — but a revoked SPA stops being
    accepted within a minute.

## API / IaC diff summary

### `api/auth.py`

- New module-level constants:
  - `_CLAIMS_CACHE_STRICT_TTL_SECONDS = 60`
  - `_STRICT_JWT_ENV = "STRICT_JWT"`
  - `_JWT_ALLOWED_APPIDS_ENV = "JWT_ALLOWED_APPIDS"`
- New helpers:
  - `_is_strict_jwt()` — re-reads the env var on every call so tests
    can `monkeypatch.setenv` without reloading the module.
  - `_claims_cache_ttl_cap()` — returns 60 s under strict, 300 s
    otherwise.
  - `_jwt_allowed_appids(api_client_id)` — defaults to `{api_client_id}`
    if `JWT_ALLOWED_APPIDS` is unset.
- `_claims_cache_put` now uses `_claims_cache_ttl_cap()` instead of the
  bare `_CLAIMS_CACHE_MAX_TTL_SECONDS` constant.
- `_validate_token` enforces the `azp`/`appid` check immediately after
  the `oid` claim presence check, gated by `_is_strict_jwt()`. The
  legacy path is untouched (constant fast-fail at the first guard).

### Infrastructure

- **No Bicep change.** `STRICT_JWT` and `JWT_ALLOWED_APPIDS` are not
  added to the Container App template yet — flipping the gate is a
  separate PR after a full release-cycle soak per charter §12a Rule 4.

### Tests

- New `api/tests/test_strict_jwt.py` (10 tests) covers both the ON and
  the OFF paths, the lazy env read, the `azp`/`appid` precedence, the
  unauthorized-appid rejection, the `JWT_ALLOWED_APPIDS` override, the
  TTL cap under strict, and the cache-put TTL ceiling effect.

## Validation evidence

```
$ uv run pytest -q api/tests/test_strict_jwt.py
..........  [100%]
10 passed in 2.39s

$ uv run pytest -q api/tests/test_security_audit_4_8.py \
                 api/tests/test_persona_matrix.py \
                 api/tests/test_sse_ticket_binding.py
74 passed in 5.76s

$ uv run pytest -q api/tests
2109 passed, 3 skipped in 34.25s

$ uv run ruff check api/auth.py api/tests/test_strict_jwt.py
All checks passed!
```

No new deployment required. Local-debug + dev-bypass flows are
unaffected because `AUTH_DEV_BYPASS=true` short-circuits before
`_validate_token` runs.

## Hardening discipline (§12a)

- [x] In scope: jwt
- [x] RBAC change is single-PR safe (no role narrowed) OR labelled
      `phase-1 of 2` / `phase-2 of 2 (see #…)` — **N/A**, no RBAC change
- [x] Persona Matrix tests pass for owner / contributor / reader /
      dev_bypass — full suite green (2109 passed)
- [x] Reader allowlist unchanged OR split-PR link: **N/A**
- [x] Capability Probe passes locally — **N/A for code-only change**
      (no Bicep change, no role change, postprovision unchanged)
- [x] New guard ships default-OFF behind `STRICT_*` / `ENFORCE_*` env
      var — `STRICT_JWT` defaults to OFF and the unset path is
      covered by `test_strict_jwt_defaults_off` and
      `test_strict_jwt_off_does_not_shorten_cache`
- [x] No `Depends(require_caller)` added to an SSE event stream
- [x] Change note (this file) summarises persona impact: Reader /
      Contributor / Owner who already authenticate successfully today
      see no change while `STRICT_JWT` is unset (the default). Once
      the gate is flipped, the only requests rejected are tokens
      issued by a non-allowlisted app — those callers would not have
      been recognised personas of our SPA in the first place.
