---
title: Service Bus multi-token outfmt via the /v1/jobs path
description: A Service Bus request message carrying blast_options is now routed to the sibling /v1/jobs endpoint (free-form, multi-token outfmt like "7 std staxids sstrand qseq sseq") instead of the XML-locked /api/v1/elastic-blast/submit, with submit-time shard-merge compatibility validation. The dashboard result parser already renders tabular output, so these jobs display in the UI unchanged.
tags:
  - blast
  - operate
---

# 2026-06-15 — Service Bus multi-token outfmt (/v1/jobs path)

## Motivation

External services wanted to enqueue a BLAST request in the sibling's `/v1/jobs`
shape — `blast_options` with a multi-token tabular `outfmt`
(`"7 std staxids sstrand qseq sseq"`) and an `extra` CLI string — and still have
the dashboard track and render the job. The Service Bus consumer only spoke the
`/api/v1/elastic-blast/submit` contract, whose `ExternalBlastOptions` pins
`outfmt` to `5` (XML, for the `Hsp_hseq`→FASTA enrichment) and has no
`additional_options` / `extra` field. So a multi-token message silently lost its
output layout and ran with the default options.

The sibling already exposes both endpoints against the **same job store**:
`/api/v1/elastic-blast/submit` (XML-locked) and `/v1/jobs` (free-form
`blast_options` incl. multi-token `outfmt` + `extra`). The dashboard result
parser already auto-branches XML (`outfmt 5`) vs tabular (`outfmt 6`/`7`) and
maps the extended columns (`staxids` / `sscinames` / `sstrand`), so the UI
already renders tabular jobs — the only missing link was the queue routing.

## User-facing change

* **A Service Bus message carrying `blast_options` is routed to `/v1/jobs`.** The
  consumer detects the `blast_options` object (the `/v1/jobs` shape) and forwards
  it to the sibling `/v1/jobs` endpoint, preserving the multi-token `outfmt` and
  `extra` verbatim. A message using the legacy `options` object
  (`ExternalBlastOptions`) keeps the existing XML path — the two are
  mutually exclusive by key name, so a producer opts in explicitly.
* **Submit-time validation rejects an un-mergeable tabular layout.** A sharded
  DB (e.g. core_nt) runs the shard-merge finalizer, which re-ranks shard hits by
  `evalue` / `bitscore` resolved by name. A tabular `outfmt` missing either is
  rejected at submit (HTTP-style 422 in the model; dead-lettered on the queue)
  instead of failing the merge minutes later. `std` (which includes both) and a
  bare numeric code pass.
* **Sharded-DB profile promotion + server-derived source apply on both paths.**
  A `core_nt` message with a missing/standard `resource_profile` is still
  promoted to `core_nt_safe`, and `submission_source=servicebus` is stamped
  server-side (a producer cannot spoof it).
* **Dashboard UI renders these jobs unchanged** — job tracking (jobstate row +
  message-flow trace), the jobs list, and the result tabs all work via the
  existing tabular-aware parser. No UI change was needed.

## Sample message (multi-token)

```json
{
  "program": "blastn",
  "db": "core_nt",
  "query_fasta": ">NC_003310.1 ...\nATG...",
  "blast_options": {
    "evalue": 0.05,
    "max_target_seqs": 100,
    "outfmt": "7 std staxids sstrand qseq sseq",
    "extra": "-word_size 28 -dust yes -soft_masking false -searchsp 32156241807668"
  },
  "resource_profile": "core_nt_safe"
}
```

## API / IaC diff summary

* New `external_blast.submit_job_v1(payload, …)` — posts to the sibling
  `POST /v1/jobs`, sharing the transport-retry + stale-token-401 self-heal
  contract of `submit_job` (delegates with `submit_path="/v1/jobs"`).
* New `ExternalBlastV1Request` / `BlastV1Options` validation models in
  [api/routes/elastic_blast.py](../../../api/routes/elastic_blast.py) — the
  dashboard mirror of the sibling `JobSubmitRequest` (Mode B) with the
  shard-merge `outfmt` guard.
* Service Bus drain ([api/tasks/servicebus/tasks.py](../../../api/tasks/servicebus/tasks.py)):
  `_is_v1_jobs_message` + `_build_v1_jobs_payload`; `_drain_handler` routes to
  `submit_job_v1` when `blast_options` is present.
* **No sibling change / image rebuild** — both sibling endpoints already exist;
  this only changes which one the queue consumer calls. Worker redeploy carries it.

## Validation evidence

* Backend: `uv run pytest -q api/tests` — **3719 passed, 3 skipped**. New suite
  `test_servicebus_v1_multitoken.py` (model accept/reject, routing detection,
  payload build + profile promotion, `submit_job_v1` posts to `/v1/jobs`).
* Lint: `uv run ruff check api` — clean.
* The XML path is unchanged: `test_servicebus_tasks.py` (legacy `options` →
  `/api/v1/elastic-blast/submit`, outfmt 5) stays green.
