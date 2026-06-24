# Service Bus result downloads — consumer format/decompress options + error bodies

## Motivation

A Service Bus completion consumer that received a `succeeded` `blast.transition`
event could download each `result_files[].download_url`, but only the stored
bytes as-is. There was no way to ask for an uncompressed copy or a re-rendered
format the way NCBI Web BLAST lets a user pick the download *format* of one
result. Compression-vs-not is a transport concern, and the right axis to expose
is **format** (the same result, re-serialised) — handled by the dashboard's
streaming gateway, never by issuing a SAS (charter §9). Downloads also needed to
work without a 401 for a consumer that already proved auth by receiving the
event, and download failures needed to surface a readable reason.

## User-facing change

* `result_files[]` entries on a succeeded event now carry **`compressed`** (the
  stored bytes are gzip) and **`media_type`** (the as-stored content type), so a
  consumer can choose a download option without a HEAD request.
* The download gateway `GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}`
  accepts two new query options (independent of the existing signed `?token=`):
  * **`?decompress=1`** — streams a gzip result inflated on the fly (memory-
    bounded; the `.gz` suffix is dropped from the filename).
  * **`?format=csv|tsv|json`** — parses the BLAST XML / tabular hits and
    re-renders them. A parse failure returns `422 result_unparseable`, an
    oversize file returns `413 result_too_large` — both as a JSON error body
    (`{"code", "message"}`), never an empty/partial download.
* **No-401 downloads**: the signed `?token=` already lets a consumer download by
  URL alone (no bearer); the new options preserve that — a download-token caller
  may also decompress/transcode the same `(job_id, file_id)`.
* The example `consume.py` now exposes `--decompress` / `--format`, records the
  gateway's JSON error body on a failed download, and surfaces `error_message`
  (the human-readable failure reason) from a `failed` event.

## API / IaC diff summary

* New `api/services/blast/result_transcode.py` — pure, streaming/bounded gunzip
  (`gunzip_stream`, `gunzip_bytes`) and XML/tabular → csv/tsv/json re-render
  (`transcode_result_bytes`) with `ResultTooLargeError` / `ResultParseError`
  surfaced as HTTP error bodies. Input capped at `TRANSCODE_MAX_BYTES` (16 MiB).
* `api/routes/elastic_blast.py` `download_external_blast_file` — added
  `decompress: bool` + `format: str` query params; fast path (no transform)
  unchanged and still streamed.
* `api/tasks/servicebus/tasks.py` `_result_files_for_event` — emits `compressed`
  + `media_type` per entry.
* `example/servicebus/consume.py` — `--decompress` / `--format`, error-body
  capture, `error_message` in the completion summary.
* Docs: `example/servicebus/README.md`,
  `docs/architecture/service-bus-examples.md`,
  `docs/operate/service-bus-result-downloads.md` updated with the new fields,
  options, and the no-bearer signed-link clarification.
* No IaC change. No new Azure resource. No SAS. Storage stays
  `publicNetworkAccess: Disabled` (bytes stream through the `api` sidecar).

## Validation evidence

* `uv run pytest -q api/tests/test_result_transcode.py` — 14 passed (gunzip
  roundtrip + oversize/non-gzip rejection; tabular/XML → csv/tsv/json; unknown
  format + oversize input rejected).
* `uv run pytest -q api/tests/test_external_blast_api.py -k download` — 11 passed
  (decompress gunzips; `format=csv` transcodes a gzip tabular; `422` body on
  malformed XML; `413` body over the cap; a signed-`?token=` caller — no bearer
  — can transcode).
* `uv run pytest -q api/tests/test_servicebus_tasks.py` — green; the
  `result_files` test now asserts `compressed` / `media_type`.
* `python3 example/servicebus/consume.py --self-test` — OK (new
  `_apply_download_options` / `_adjust_filename` + `error_message` assertions).
* Docs frontmatter guard — OK, 58 navigated pages.

## Out of scope (tracked separately)

Producing a format-agnostic archive (BLAST `outfmt 11` ASN.1) at job time so the
gateway could re-render to *any* NCBI format (XML2, JSON2, pairwise text, GenBank)
via `blast_formatter` — the long-term option from the design discussion — is
tracked as a separate GitHub issue ([#77](https://github.com/dotnetpower/elb-dashboard/issues/77))
(it requires an elastic-blast merge-step change and extra storage, unlike this
gateway-only work).
