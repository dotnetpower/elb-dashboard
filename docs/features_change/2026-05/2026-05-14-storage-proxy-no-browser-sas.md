# 2026-05-14 — Browser ↔ Storage proxy: api sidecar streams uploads and downloads

## Motivation

With Storage `publicNetworkAccess=Disabled` enforced from day 1, any SAS token
the API hands to the browser is useless: the SAS-bearing URL points at a
storage hostname that resolves to a private IP the browser cannot reach. The
user asked for the consequence of that fact to be made explicit:

> Can the Container App act as the proxy for file downloads instead of issuing
> SAS tokens to the browser? Can uploads work the same way?

Yes — and it is the only design that is consistent with the day-1
private-storage invariant. The migration plan now states it as the contract,
not as a fallback.

## User-facing change

None at runtime (planning document update). For users this is invisible: the
browser still uploads queries and downloads results through the same SPA
buttons; only the URL the browser hits changes from
`https://<account>.blob.core.windows.net/.../...?sv=...&sig=...` to
`https://<api-host>/api/blast/jobs/{job_id}/{queries|results}/{name}`.

## Architecture diff summary

| Area | Before (mixed) | Now (proxy-only) |
|------|----------------|------------------|
| Browser → Storage upload | Either multipart to API or browser PUT to a SAS-signed Storage URL | Browser PUT to `/api/blast/jobs/{job_id}/queries/{name}`; api sidecar streams to Storage with managed identity using `stage_block` + `commit_block_list` |
| Browser ← Storage download | Either API endpoint or browser GET on a SAS-signed Storage URL | Browser GET on `/api/blast/jobs/{job_id}/results/{name}`; api sidecar streams 1 MiB chunks from Storage to the response, passes through `ETag`, `Content-Type`, `Content-Length`, `Last-Modified`, supports `Range` → `206 Partial Content` |
| Browser-bound SAS | Allowed for "explicit result-download workflows" | **Forbidden.** No `generate_blob_sas`, `generate_container_sas`, or `BlobClient.url` for any browser-bound response. CI grep gate enforces this. |
| Memory profile of api sidecar during transfer | Implicit | Explicit guardrails: streaming with `chunk_size=1 MiB` (download) / `4 MiB` block (upload); RSS test in CI |
| Concurrency | Implicit | Explicit: per-replica semaphore (initial: 4 simultaneous transfers), excess gets `429 Too Many Requests` with `Retry-After` |
| Upload size cap | Implicit | Explicit: `413 Payload Too Large` above 256 MiB. Larger inputs (NCBI database imports, etc.) are server-side worker tasks, not browser-driven. |
| Container Apps 240s timeout | Not addressed | Addressed: downloads support `Range`/`206`; SPA uses range requests for results > ~200 MiB |

## Why not user delegation SAS

Documented in the new "Why not user delegation SAS?" subsection. Three reasons:

1. The Storage endpoint is unreachable from the public internet, so a SAS to
   the public hostname does not work.
2. Issuing a SAS to a public hostname (some bypass that re-exposes the
   account) violates the day-1 private-storage rule.
3. Removing browser-bound SAS removes a class of token-leak incidents (logs,
   browser history, screenshots, support tickets).

The cost — api sidecar pays CPU/bandwidth per transfer — is acknowledged in
the Risks table. Escalation path if the bundled topology saturates: split api
into its own Container App with `maxReplicas` > 1, **not** re-introduce SAS.

## Files changed

- `docs/container-apps-migration.md`:
  - "Storage Network Isolation → Rule 3" rewritten: SPA never talks to Storage
    directly; api sidecar is the sole client; no SAS to the browser ever.
  - "What this forbids" tightened: forbids **any** SAS to the browser, not
    just SAS that depends on public access.
  - **New section "Browser ↔ Storage Proxy (No SAS to the Browser)"** placed
    between the Storage Network Isolation section and Target Architecture.
    Defines: rules, download contract (`GET .../results/{name}`), upload
    contract (`POST .../queries`), why-not-SAS rationale, and verification
    tests (round-trip integrity, RSS bound, concurrency limit, Range support,
    CI grep gate).
  - Storage Plan updated: SAS line replaced with proxy-only rule; internal
    SAS usage allow-listed and time-bounded.
  - Cutover Checklist gains three new rows: browser upload proxy works,
    browser download proxy supports Range, CI grep blocks new browser-bound
    SAS.
  - Risks table: storage-public-access risk row updated to mention the proxy;
    two new rows for "API sidecar saturated by proxy traffic" and "Large
    download exceeds 240s Container Apps timeout".
- `README.md`: Architecture Planning bullet now mentions the proxy contract.

## Code consequences (follow-up tickets)

These are not done in this PR (planning only):

1. Remove every browser-bound SAS issuer in
   [api/services/storage_data.py](api/services/storage_data.py),
   [api/routes/data_plane.py](api/routes/data_plane.py),
   [api/routes/blast_jobs.py](api/routes/blast_jobs.py).
2. Add streaming upload endpoint
   `POST /api/blast/jobs/{job_id}/queries/{name}` using FastAPI
   `request.stream()` + `BlobClient.stage_block` / `commit_block_list`.
3. Add streaming download endpoint
   `GET /api/blast/jobs/{job_id}/results/{name}` with `Range` passthrough,
   `Accept-Ranges: bytes`, and 1 MiB chunk iteration.
4. Add a per-replica `asyncio.Semaphore(4)` around proxy handlers; `429` on
   acquire timeout.
5. Add a CI grep gate (`scripts/dev/check-no-browser-sas.sh`) that fails on
   `generate_blob_sas|generate_container_sas|BlobClient\.url` outside the
   allow-list.
6. Update the SPA to call the new endpoints instead of building Storage URLs
   client-side.
7. Add the four verification tests listed in the doc to the api `pytest`
   suite.

## Validation evidence

Documentation-only change. Verified the doc has no remaining "issue SAS to the
browser" guidance:

```bash
grep -nE "(user delegation SAS|SAS).*(browser|client|user)|browser.*SAS" \
  docs/container-apps-migration.md
```

The matches that remain are inside the new "Why not user delegation SAS?"
explanation and the forbid/risk rows that name what is being removed. No
active recommendation suggests handing a SAS to the browser.
