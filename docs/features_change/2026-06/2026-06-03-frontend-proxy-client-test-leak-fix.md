---
title: Fix frontend-proxy test client leak breaking openapi-gating test in CI
description: The recording AsyncClient stub leaked across test files, making /openapi.json return 200 and failing test_openapi_hidden_by_default in the serial CI run.
tags:
  - contributor
  - security
---

# Frontend-proxy test client leak fix

## Motivation

The GitHub Actions **Tests** workflow (`uv run pytest -q api/tests`, serial) was
red on `api/tests/test_security_headers.py::test_openapi_hidden_by_default` with
`AssertionError: assert 200 != 200`, while the local parallel loop (`-n auto`)
stayed green. The failure was a test-isolation leak, not a product regression.

## Root cause

`api/routes/frontend_proxy.py` caches its upstream `httpx.AsyncClient` in a
module-level global `_client`. The helper `_patch_frontend_client` in
`api/tests/test_security_audit_bundle.py` replaced it with a `_RecordingAsyncClient`
stub via a **bare assignment** (`frontend_proxy._client = _RecordingAsyncClient()`)
instead of `monkeypatch.setattr`. The stub returns a hard-coded `200` for every
request and was never restored, so it leaked past the file.

In the serial CI ordering, `test_security_audit_bundle.py` runs before
`test_security_headers.py`. When `test_openapi_hidden_by_default` builds a fresh
app with `openapi_url=None` and requests `/openapi.json`, the path falls through
to the catch-all reverse proxy — which used the leaked recording client and
returned `200`, breaking the assertion that the spec is hidden by default. Under
`-n auto` the two files land on different workers, so the leak was invisible
locally.

## User-facing change

None. Test-only fix; production behaviour is unchanged.

## API/IaC diff summary

- `api/tests/test_security_audit_bundle.py`: `_patch_frontend_client` now uses
  `monkeypatch.setattr(frontend_proxy, "_client", _RecordingAsyncClient())` so the
  module-global client is automatically restored after each test.

## Validation evidence

- Repro (serial, CI parity): `uv run pytest -q api/tests` →
  `1 failed, 2447 passed` before the fix (FAILED `test_openapi_hidden_by_default`).
- After the fix: `uv run pytest -q api/tests` → `2448 passed, 3 skipped`.
- `uv run ruff check api/tests/test_security_audit_bundle.py` → `All checks passed!`
