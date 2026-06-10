# App Insights: attach the failure reason to 4xx/5xx request spans

## Motivation

Follow-up to the App Insights error audit. An operator looking at a failed
request in App Insights could see the `resultCode` (e.g. `400`/`404`/`422`/`500`)
but **no detail about *why*** — the request row carried no `customDimensions`
explaining the failure, and 4xx requests were not even flagged as failed.

Two reasons:

1. The FastAPI OpenTelemetry instrumentor records a span per request but treats
   4xx as a successful span (HTTP semantics: a client error is not a server
   fault). It never attaches the failure reason.
2. Our routes return 4xx via `raise HTTPException(...)`, which the app's
   `StarletteHTTPException` handler converts to a clean `JSONResponse`. That
   never reaches the middleware's `except` block, so nothing landed in the
   `exceptions` table either.

Net result: a 4xx/5xx showed up as a bare status code with no diagnosable
context.

## User-facing change

None in the SPA. For operators, every 4xx/5xx request now carries
`customDimensions` on its App Insights `requests` row:

* `elb.error.type` — `http_<code>` for `HTTPException`, `validation_error` for
  422, or the exception class name for an unhandled 500.
* `elb.error.detail` — the sanitised failure message (structured-detail
  `message` preferred; secrets/SAS/subscription ids redacted via `sanitise`).
  For 422 it is the offending field **locations** only, never the submitted
  values.
* `elb.error.status_code` — the HTTP status (also on `resultCode`).
* `elb.request.id` — the same `x-request-id` the client/log line carries, for
  correlation.

The `elb.*` namespace is deliberate: the ASGI instrumentor owns the standard
`error.type` / `http.response.status_code` keys and sets `error.type` to the
bare status string under the new HTTP semantic-convention mode — and it runs
AFTER our exception handler, so writing our richer value to the standard key
would be silently overwritten. `elb.*` keys are never touched by the
instrumentor.

5xx additionally flips the span status to `ERROR` so the Failures blade and
`requests | where success == false` light up. 4xx stays a non-error span (it is
a client problem) but is now fully queryable, e.g.:

```kusto
requests
| where customDimensions has "elb.error.type"
| project timestamp, name, resultCode,
          errorType=tostring(customDimensions["elb.error.type"]),
          detail=tostring(customDimensions["elb.error.detail"]),
          rid=tostring(customDimensions["elb.request.id"])
| order by timestamp desc
```

## API / IaC diff summary

- `api/app/telemetry.py` — new `annotate_error_span(status_code, error_type,
  detail, request_id)` writing `elb.*` attributes (never the instrumentor-owned
  standard keys). No-op when telemetry is uninitialized (non-recording span) or
  on any error; only 5xx sets the span status to ERROR. Caller must
  pre-sanitise `detail` (length-capped here as a backstop).
- `api/main.py` — new module helpers `_error_detail_text` (sanitise + extract a
  short message from str/dict details) and `_annotate_error_span_safe` (lazy
  import + broad guard). The three exception handlers
  (`StarletteHTTPException`, `RequestValidationError`, unhandled `Exception`)
  now call the annotator. Response status/body are unchanged.
- No IaC change. No new dependency (uses the already-pinned OpenTelemetry SDK).

## Validation evidence

- `uv run pytest -q api/tests` → 3211 passed, 3 skipped.
- New `api/tests/test_error_span_annotation.py` (4): 404 + 422 handler wiring,
  SAS redaction in `_error_detail_text`, empty → None.
- New `annotate_error_span` unit cases in `api/tests/test_telemetry_init.py`
  (4): 4xx attaches detail without ERROR status, 5xx sets ERROR, no-op when not
  recording, never raises.
- `uv run ruff check` clean.

## Note

This change makes the *next* failure diagnosable; it does not backfill the 0
historical 4xx/5xx (there were none in 30 days — see the companion audit note).
It is an `api/` code change validated by pytest; it takes effect when the api
image carrying it is next deployed.
