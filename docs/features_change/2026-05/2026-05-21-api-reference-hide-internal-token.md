# API Reference — hide internal-only header parameters

## Motivation

The deployed `elb-openapi` service declares `X-ELB-Internal-Token` as a
`Header()` parameter on `POST /v1/jobs` to authenticate trusted
`submission_source` values (`dashboard` / `terminal` / `system`). The
dashboard's API Reference page rendered every spec parameter verbatim,
so external operators saw `X-ELB-Internal-Token` listed under the
endpoint's `Parameters` section as if they were expected to supply it.

In reality:

* External clients cannot ever obtain this token — it lives in the
  dashboard's Container App secret `ELB_OPENAPI_INTERNAL_TOKEN`.
* The dashboard's `Try` proxy ([api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py))
  intentionally forwards only `X-ELB-API-Token`, never the internal
  token, so the field was non-functional even from inside the UI.
* Backend tasks that legitimately need the trust channel call
  [api/services/external_blast.py](../../../api/services/external_blast.py)
  `_headers()`, which attaches both tokens server-side.

The visible field was misleading without being useful.

## User-facing change

The API Reference page no longer lists `X-ELB-Internal-Token` in the
`Parameters` section of any endpoint. The Try form, copy-as-curl text,
and the rendered parameter table all derive from the parsed spec, so a
single filter at parse time keeps the three views consistent.

## Implementation

[web/src/pages/apiReference/spec.ts](../../../web/src/pages/apiReference/spec.ts)
introduces a `HIDDEN_HEADER_PARAMS` allowlist (currently
`x-elb-internal-token`, case-insensitive) and applies it to
`detail.parameters` inside `parseSpec`. Header parameters in the set are
dropped; non-header parameters and other headers are preserved
unchanged. The set is the only place to add future internal-only
headers.

No backend change. The proxy already does the right thing, and the
`external_blast` SDK helper continues to attach both tokens for
server-initiated calls.

## Validation

* `cd web && npm run build` — clean, 6.99 s.
* Spec-driven UI: by inspection, the `Parameters` section of
  `POST /v1/jobs` now has only `submission_source` and the body, with
  no `X-ELB-Internal-Token` row.
