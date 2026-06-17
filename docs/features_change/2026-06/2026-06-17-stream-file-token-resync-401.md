# Fix stream_file 401 — result download self-heals stale openapi token

## Motivation

A live end-to-end test (Service Bus request → BLAST run → completion event →
`download_url`) surfaced that the result-file `download_url` returned **HTTP
401** from the dashboard, with the upstream elb-openapi reporting `missing or
invalid X-ELB-API-Token`.

Root cause: every sibling call that talks to the deployed `elb-openapi` pod
(`get_job`, `list_jobs`, Service-Bus-driven `submit_job`) goes through
`_request_with_token_resync`, which on a 401 re-reads the live admin token from
the cluster and retries once — recovering from the common failure mode where a
control-plane redeploy or AKS restart wipes the dashboard's *ephemeral* runtime
token cache while the elb-openapi pod keeps its minted token. But
`api.services.external_blast.stream_file` — the function that serves both the
Service Bus completion `download_url`
(`/api/v1/elastic-blast/jobs/{job_id}/files/{file_id}`) and the dashboard
Results "download" button — built its own httpx client and sent the request
**without** the resync-on-401 retry. So after any redeploy/restart, file
downloads 401'd permanently even though submit/list/status had already self-
healed.

## User-facing change

Result-file downloads (the Service Bus completion `download_url` and the
Results page download) now self-heal a stale-token 401 exactly once, identically
to every other sibling call: on a 401 the dashboard re-reads the live token from
the cluster, then reopens the stream with the recovered token. No more spurious
401 on the first download after a control-plane redeploy or cluster restart.

## API / IaC diff summary

Backend-only, no API/IaC change. Same upstream contract; only the retry-on-401
behaviour was added.

- `api/services/external_blast.py` — `stream_file` now opens via a small
  `_open(token)` helper and, on a 401, calls the existing
  `_resync_token_after_401()` and reopens once with the recovered token before
  `raise_for_status()`. Streaming responses can't be retried in place, so the
  first (401) response/client are closed and a fresh client is opened.
- `api/tests/test_external_blast_api.py` — two tests: 401→resync→200 retry path
  (asserts the retry carries the healed token and the body streams), and the
  no-token-recovered path (surfaces the 401).

## Validation evidence

- `uv run pytest -q api/tests/test_external_blast_api.py` → 106 passed.
- `uv run ruff check` on both files → clean.
- Live: reproduced via a real Service Bus E2E (16S blastn, job `73c663fe9c7b`)
  whose completion `download_url` 401'd before the fix. The completion event
  correctly carried `result_files[0] = {file_id: result-001, name:
  batch_000-blastn-16S_ribosomal_RNA.out.gz, format: blast_xml, size: 25565,
  download_url: …/jobs/73c663fe9c7b/files/result-001}`; the 401 was on the
  download step only. Post-deploy verification of the downloaded bytes is the
  follow-up step.
