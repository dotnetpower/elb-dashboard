# 2026-05-21 — API Reference: binary responses auto-download

## Motivation

`GET /v1/jobs/{job_id}/results` and other download endpoints return a ZIP /
binary archive. The API Reference Try It panel was calling
`Response.text()` on every reply and dumping the raw bytes into the response
viewer, which produced an unreadable wall of garbled characters and gave
the impression that the call had failed. The Try It "Download results" hint
also could not actually deliver a file because the OpenAPI proxy did not
forward the `Content-Disposition` header from the upstream `elb-openapi`
service.

## User-facing change

When the upstream response advertises a binary `Content-Type` (anything
outside `text/*`, `application/json`, `application/problem+json`, the
`+json`/`+xml` suffixes, `application/xml`, `application/javascript`, or
`application/x-www-form-urlencoded`):

- The Try It panel now reads the body as a `Blob` and triggers a regular
  browser download instead of decoding it as text.
- The response viewer shows a small four-line summary (filename, size,
  content-type) so users immediately see the file landed on disk:

  ```
  // Binary response downloaded automatically.
  // file:         merged_results.zip
  // size:         12.4 MiB
  // content-type: application/zip
  ```

- JSON / text responses are unchanged.

The filename is taken from `Content-Disposition` when present (including the
RFC 5987 `filename*=UTF-8''…` form). When the header is missing the
executor falls back to the final path segment plus a content-type-based
extension (`zip`, `gz`, `tar`, `pdf`, `fa`, …) and `.bin` as a last
resort.

## API / IaC diff summary

- `api/routes/aks/openapi.py` — `aks_openapi_proxy` now also forwards the
  upstream `Content-Disposition` header (still on the same explicit
  whitelist as `Content-Type`; nothing else is forwarded).
- `web/src/hooks/useOpenApiExecutor.ts`
  - `OpenApiExecutionResponse` gained an optional `download` field
    (`{ filename, bytes, contentType }`).
  - New `readResponseForViewer(resp, targetPath)` dispatches to a binary
    download path or the previous text path based on `isBinaryContentType`.
  - New exported helpers `isBinaryContentType`, `pickDownloadFilename`,
    `formatBinarySummary` are unit-tested directly.
- No infra / Bicep changes.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_proxy_route.py` →
  `8 passed in 1.41s` (existing 7 + new
  `test_openapi_proxy_forwards_zip_download_headers`).
- `npm --prefix web run test -- --run src/hooks/useOpenApiExecutor.test.ts`
  → `8 tests passed` (2 existing path-builder tests + 6 new binary-handling
  tests).
- `npm --prefix web run lint` → no warnings.
- `npm --prefix web run build` → success (existing large-chunk advisory
  only).
- Manual: `GET /v1/jobs/{id}/results` Try It now triggers a
  `merged_results.zip` download and the response viewer shows the binary
  summary instead of garbled bytes.
