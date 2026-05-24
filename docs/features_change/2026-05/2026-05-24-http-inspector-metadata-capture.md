# HTTP Inspector Metadata Capture

## Motivation

The sidecar topology card could show Browser -> api traffic while the HTTP
request inspector stayed empty. The root cause was that the detail buffer used a
single environment switch for both lightweight request rows and expensive
request/response body buffering. When body capture was disabled for performance,
the inspector recorded no rows at all, which made the live traffic counter and
the inspector disagree.

## User-facing change

The HTTP inspector now records lightweight metadata rows by default for recent
non-streaming API requests, including high-volume polling GETs. Request and
response bodies remain opt-in via `REQUEST_DETAIL_CAPTURE_ENABLED=true`, and
`REQUEST_DETAIL_CAPTURE_ENABLED=false` still disables the detail buffer
entirely.

The empty state and button tooltip now describe that streaming and
self-inspection routes are filtered.

## API / IaC diff summary

- Added a metadata-recording predicate in `api.app.inspector` while keeping the
  existing body-capture predicate for capped body buffering.
- Updated `RequestIdMiddleware` so unset `REQUEST_DETAIL_CAPTURE_ENABLED`
  records metadata only, `true` records metadata plus capped bodies, and `false`
  records nothing in the detail buffer.
- No route schema or Bicep changes.

## Validation evidence

- `uv run pytest -q api/tests/test_inspector_exclude.py api/tests/test_request_metrics_detail.py`
- `uv run ruff check api/app/inspector.py api/app/middleware.py api/routes/monitor/metrics.py api/tests/test_inspector_exclude.py api/tests/test_request_metrics_detail.py`
- Local smoke: POST probe against `http://127.0.0.1:8085/api/resources/_inspector_probe` previously left `/api/monitor/sidecar-requests` at `count: 0`; after an API restart it should produce a metadata row even without body capture enabled.