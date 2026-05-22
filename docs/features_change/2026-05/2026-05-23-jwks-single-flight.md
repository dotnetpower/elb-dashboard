# JWKS single-flight election

## Motivation
`_get_jwks_client(tenant_id)` had no concurrency control around the
cache miss path. On a cold start (process boot, JWKS TTL expiry,
`reset_caches()` call) every concurrent authenticated request paid the
full OIDC discovery + JWKS fetch round-trip in parallel — a textbook
thundering herd against `login.microsoftonline.com`.

## User-facing change
None functionally. Cold-start auth latency stays the same for the one
elected leader; concurrent followers wait on the leader's `Event` and
return the freshly cached `PyJWKClient` without their own HTTPS call.

## API / IaC diff
* `api/auth.py`
  * Added `_JWKS_INFLIGHT: dict[tenant_id, threading.Event]` +
    `_JWKS_INFLIGHT_LOCK`.
  * `_get_jwks_client` elects a leader inside the inflight lock,
    builds the client outside the lock, then signals + cleans up the
    inflight entry in `finally`.
  * Non-leaders wait up to 15 s on the leader's event then check the
    cache; on timeout (leader crashed) they fall through and elect
    themselves.

## Validation
* `uv run pytest -q api/tests/test_auth_caching.py` — 9 passed.
* `uv run ruff check api/auth.py` — clean.
