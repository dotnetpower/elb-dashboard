# Service Bus examples

Standalone, single-file Python examples that reproduce the **exact JSON contract**
the dashboard uses for its optional Azure Service Bus BLAST integration. Each
file runs on its own and mirrors a real code path in `api/`:

| File | Mirrors | What it does |
| --- | --- | --- |
| [`send_request.py`](send_request.py) | `api.services.service_bus.send_request` | Producer — enqueues a BLAST request message onto the `elastic-blast-requests` queue. |
| [`monitor.py`](monitor.py) | `api.services.service_bus.entity_counts` + `peek_requests` | Monitoring — reads runtime counts (queue + topic subscriptions) and non-destructively peeks messages. |
| [`consume.py`](consume.py) | `api.services.service_bus.drain_requests` / completion-topic subscriber | Consumer — receives and settles request-queue messages, or subscribes to the completion topic. |

## Message contracts

### Request message (producer → `elastic-blast-requests` queue)

Envelope: `content_type="application/json"`, `subject="blast.request"`,
`correlation_id=<external_correlation_id>`.

XML-locked body (`/api/v1/elastic-blast/submit`, `outfmt` fixed to `5`):

```json
{
  "program": "blastn",
  "db": "core_nt",
  "query_fasta": ">query1\nACGT...",
  "taxid": 9606,
  "is_inclusive": true,
  "options": { "outfmt": 5, "word_size": 28, "dust": true, "evalue": 0.05, "max_target_seqs": 500 },
  "resource_profile": "standard",
  "external_correlation_id": "<hex>",
  "request_id": "<caller pass-through>"
}
```

Free-form body (`/v1/jobs`, multi-token tabular `outfmt`) — carries
`blast_options` instead of `options`, which is the routing key the consumer uses:

```json
{
  "program": "blastn",
  "db": "core_nt",
  "query_fasta": ">query1\nACGT...",
  "blast_options": { "outfmt": "7 std staxids sstrand qseq sseq" },
  "external_correlation_id": "<hex>"
}
```

### Completion event (`elastic-blast-completions` topic)

```json
{
  "event": "blast.transition",
  "event_id": "<sha256(correlation_id:status)>",
  "attempt": 1,
  "external_correlation_id": "<hex>",
  "openapi_job_id": "<job id>",
  "status": "queued | running | succeeded | failed",
  "ts": "2026-06-17T00:00:00+00:00",
  "result_ref": {
    "api": "GET /api/v1/elastic-blast/jobs/{id}",
    "files": "GET /api/v1/elastic-blast/jobs/{id}/files/{file_id}"
  },
  "result_files": [
    {
      "file_id": "merged_results.out.gz",
      "name": "merged_results.out.gz",
      "format": "blast_tabular",
      "size": 12345,
      "download_url": "https://<dashboard-host>/api/v1/elastic-blast/jobs/{id}/files/merged_results.out.gz"
    }
  ],
  "request_id": "<optional pass-through>",
  "error_code": "<optional, on failed>"
}
```

`result_files` is present only on a **succeeded** event. Each `download_url`
points at the dashboard's authenticated file-streaming gateway — a consumer
downloads by calling it with a **bearer token** (the `api` sidecar streams the
bytes). It is **never** a Storage SAS URL or a direct blob URL (charter §9).

Subscribers dedupe on the stable `event_id` because Service Bus delivery is
at-least-once.

## Configuration (environment variables)

| Variable | Default |
| --- | --- |
| `SERVICEBUS_NAMESPACE_FQDN` | `sb-elb-dashboard-krc.servicebus.windows.net` |
| `SERVICEBUS_REQUEST_QUEUE` | `elastic-blast-requests` |
| `SERVICEBUS_COMPLETION_TOPIC` | `elastic-blast-completions` |
| `SERVICEBUS_COMPLETION_SUBSCRIPTION` | `default` |

## Auth & RBAC

All three use `DefaultAzureCredential` (interactive `az login` or a managed
identity). Required namespace role per action:

* send → **Azure Service Bus Data Sender**
* peek / receive → **Azure Service Bus Data Receiver**
* runtime counts (`monitor.py` management call) → **Azure Service Bus Data Owner**

## Running

```bash
# Offline structural self-test (no Azure, no network) — validates the JSON contract:
python send_request.py --self-test
python monitor.py      --self-test
python consume.py      --self-test

# Build & print a request without sending it:
python send_request.py --dry-run

# Live (needs az login + the RBAC above):
python send_request.py --db core_nt --program blastn
python monitor.py --peek 5
python consume.py --source requests --settle abandon   # peek-and-return, safe
python consume.py --source completions --subscription default --max 10

# End-to-end: receive completion events and download result files via download_url.
# Provide a bearer token for the dashboard (ELB_BEARER_TOKEN), or let the script
# acquire one via `az account get-access-token` by setting ELB_API_CLIENT_ID:
ELB_API_CLIENT_ID=<api-client-id> \
  python consume.py --source completions --download --download-dir ./out
```

> `consume.py --source requests` with the default `--settle auto` **completes**
> (removes) messages. Use `--settle abandon` against the live queue so the real
> worker still processes them, or point the scripts at a throwaway namespace.

Dependencies: `azure-servicebus`, `azure-identity` (already in the project venv —
run with `uv run python <file>`).
