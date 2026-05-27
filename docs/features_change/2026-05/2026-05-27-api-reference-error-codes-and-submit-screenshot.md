# API Reference: submit screenshot + dedicated error code section

## Motivation

The API Reference page rendered every `POST /v1/jobs` response code in its endpoint card, but the published user-guide page only listed a short symptom table in **Troubleshooting**. Clients reading the docs without opening the live page could not tell what each `4xx` / `5xx` row meant, and the page never said that a `500` response body actually carries the failure `detail` — leading some integrations to silently swallow server errors.

## User-facing change

- Embedded `docs/images/screenshots/api-jobs-submit.png` directly under the **Submit Example: `POST /v1/jobs`** heading so readers see the same response-code list (`202`, `400`, `401`, `409`, `422`, `429`, `500`) that the live endpoint card shows.
- Added a new **Error Codes** section with a per-code table covering label, when it is returned, the JSON body shape, and the client action. The `500 RuntimeFailure` row explicitly documents that the response body always includes `detail` and `request_id`, with a follow-up admonition (`!!! note "500 always carries content"`) reinforcing the point.
- Rewrote the **Troubleshooting** table intro to be symptom-first and cross-link to the new section; replaced the catch-all `5xx` row with one that tells clients to read the response body rather than treat `5xx` as opaque.
- Added the **Error codes** anchor to the page's Quick jumps callout.

## API / IaC diff summary

Documentation-only change. No FastAPI route, OpenAPI spec, Bicep, or backend behaviour was modified.

- `docs/user-guide/api-reference.md`: added image, new `## Error Codes` section + admonition, refreshed Troubleshooting intro and `5xx` row, updated Quick jumps.

## Validation evidence

- `grep -n "api-jobs-submit\|Error Codes\|RuntimeFailure\|## Troubleshooting" docs/user-guide/api-reference.md` shows the new image reference (line 118), the new `## Error Codes` heading (501), the `500 RuntimeFailure` row (523), the admonition (528), and the rewritten `## Troubleshooting` section (530–532).
- Image asset already exists at `docs/images/screenshots/api-jobs-submit.png` (no new binary to commit).
- Anchors `#error-codes`, `#submit-example-post-v1jobs`, and `#troubleshooting` follow MkDocs Material's default slugifier and match the cross-links used in the page.
