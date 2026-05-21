# Results endpoint `?content=` mode (full / merged / xml)

## Motivation

`GET /v1/jobs/{job_id}/results` always returned a ZIP of every shard's
`*.out.gz` / `*.out` file. When a job has been sharded and merged by the
merger CronJob, the user only wants the merged outfmt=5 XML, but had to
download tens of MB of per-shard files and find `merged_results.out.gz`
inside the ZIP by hand. The new "binary download" path in the API
Reference page (2026-05-21-api-reference-binary-download.md) made this
obvious — once Try It started saving the ZIP, users immediately asked
"how do I get just the merged XML?".

## User-facing change

`/v1/jobs/{job_id}/results` now accepts a `content` query parameter:

| `content` | Response body | Filename | Notes |
| --------- | ------------- | -------- | ----- |
| `full` (default) | `application/zip` | `blast-results-<job_id>.zip` | Backward-compatible: every shard's `*.out.gz`/`*.out`. |
| `merged` | `application/zip` | `blast-results-<job_id>-merged.zip` | Contains only `merged_results.out.gz`. `404` if merger has not uploaded yet. |
| `xml` | `application/xml` | `blast-results-<job_id>.xml` | Gunzipped BLAST XML (outfmt=5) from `merged_results.out.gz`. `404` if merger has not uploaded yet. |

The default keeps existing integrations working unchanged. The API
Reference Try It form now shows `content` as a dropdown (enum-aware
parameter rendering), so users see the three options directly.

## API / IaC diff summary

Cross-repo change.

### `dotnetpower/elastic-blast-azure` (`docker-openapi/app/main.py`)

- Add `Query` to the `fastapi` import line, `Literal` to the `typing`
  import line.
- Replace `download_results(job_id: str)` with
  `download_results(job_id, content: Literal["full", "merged", "xml"] = Query(default="full", ...))`.
  - `full` branch is the original implementation, unchanged.
  - `merged` / `xml` branches azcopy-include-pattern only
    `merged_results.out.gz`, then either re-zip that single file
    (`merged` mode) or `gzip.open` + `shutil.copyfileobj` to a plain
    XML file (`xml` mode). Both `404` when the merger has not uploaded
    yet (the merger is best-effort).
- `_cleanup_tmp` is already variadic, so the outer `except` now passes
  all three temp paths (`work_dir`, `zip_path`, `xml_path`).
- New module-level constant `_MERGED_RESULTS_BLOB = "merged_results.out.gz"`.

### `elb-dashboard` (this repo)

- `web/src/pages/apiReference/types.ts` — `SpecParam.schema` gains an
  optional `enum?: unknown[]`. The OpenAPI spec already carried enum
  for `content`; the dashboard type just did not surface it.
- `web/src/pages/apiReference/EndpointCard.tsx` — when a query
  parameter's schema has an `enum`, render a `<select>` with an empty
  "<default> (default)" option plus one option per enum value, instead
  of the generic text `<input>`. Path parameters remain text inputs.

No infra change. No new dependency. Backend (`api/`) untouched —
`/v1/jobs/{job_id}/results` is upstream-only; the dashboard proxies it
through `/api/aks/openapi/proxy?path=...`.

## Validation

- Sibling repo syntax: `python -c "import ast; ast.parse(open('docker-openapi/app/main.py').read())"` → AST OK.
- This repo:
  - `uv run pytest -q api/tests` — backend untouched, no run needed for this slice
    (the existing 8 proxy tests still pass per the previous change).
  - `cd web && npx tsc --noEmit -p tsconfig.json` — clean (only fix
    was teaching `SpecParam.schema` about `enum`).
  - `cd web && npm run lint` — clean.
  - `cd web && npm run build` — succeeds.
  - `cd web && npx vitest run src/pages/apiReference src/hooks/useOpenApiExecutor.test.ts`
    — 13 passed, 0 failed.
- Manual plan (after sibling image redeploy): in API Reference, open
  `GET /v1/jobs/{job_id}/results`, confirm `content` shows up as a
  dropdown with `full`, `merged`, `xml`. Submitting `xml` against a
  merged job downloads `blast-results-<job_id>.xml` (XML viewer in
  browser). Submitting `merged` downloads a small zip containing only
  `merged_results.out.gz`. Submitting `full` (or leaving blank) keeps
  the original ZIP behaviour.

## Rollout notes

- Cross-repo: per `.github/copilot-instructions.md` §13, the sibling
  patch and this dashboard tweak ship together. The dashboard change is
  safe even without the sibling change (current spec has no `enum` for
  `content`, so the existing text input renders as before).
- No charter §13 redeploy rule violation: no `azd provision`, no
  `quick-deploy.sh`, no `az acr build` performed here. The sibling
  image will pick up the new behaviour through its own OpenAPI
  deployment flow.
