# 2026-05-23 — api/app/ subpackage (main.py split)

## Motivation
Phase B of the SRP cleanup. `api/main.py` had grown to 615 LOC and was
juggling four responsibilities: inspector rules + JWT helpers + the giant
RequestIdMiddleware body + the lifespan context + app composition. Pull the
helpers into a dedicated `api/app/` package so `main.py` shrinks back to a
thin wiring file (~220 LOC).

## Diff
- New package: `api/app/`
  - `inspector.py` — capture path lists + `_inspector_should_capture` predicate
  - `jwt_utils.py` — `_decode_jwt_upn`, `_extract_client_ip`
  - `middleware.py` — `RequestIdMiddleware` (~225 LOC of body buffering /
    metrics emission)
  - `lifespan.py` — `_lifespan` context (credential warm-up + subscriber start
    + clean shutdown of broadcaster, frontend proxy client, httpx pool)
- `api/main.py` now: docstring + logging setup + `create_app()` + `app`,
  with a re-export of `_inspector_should_capture` for the two tests that
  import it from `api.main`.
- LOC: `main.py` 615 → 219; new `app/*.py` total ≈ 459 (with docstrings,
  comments, and headers — same code, more readable).

## Validation
- `uv run pytest -q api/tests` → 1260 passed in 60.63s
- `uv run ruff check api` → All checks passed
