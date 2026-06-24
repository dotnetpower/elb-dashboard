# Service Bus examples

Small, standalone Python scripts that show the JSON contract the dashboard uses
for its optional Azure Service Bus BLAST integration. Each file runs on its own
and mirrors a real code path in `api/`.

| File | Mirrors | What it does |
| --- | --- | --- |
| [`send_request.py`](send_request.py) | `api.services.service_bus.send_request` | Producer — put one BLAST request on the `elastic-blast-requests` queue. |
| [`consume.py`](consume.py) | `api.services.service_bus.drain_requests` | Consumer — receive request messages and settle them (complete / abandon / dead-letter). |
| [`monitor.py`](monitor.py) | `api.services.service_bus.entity_counts` + `peek_requests` | Monitoring — read queue counts and non-destructively peek messages. |
| [`load_test.py`](load_test.py) | — | Burst tool (not a basic example) — enqueue many requests for a load test. |

## Message contract

Envelope: `content_type="application/json"`, `subject="blast.request"`,
`correlation_id=<external_correlation_id>`.

Body — two shapes the consumer routes on:

```jsonc
// xml mode → /api/v1/elastic-blast/submit (outfmt locked to 5)
{
  "program": "blastn",
  "db": "core_nt",
  "query_fasta": ">query1\nACGT...",
  "external_correlation_id": "<hex>",
  "options": { "outfmt": 5, "evalue": 0.05, "max_target_seqs": 500 }
}

// v1 mode → /v1/jobs (free-form tabular outfmt under blast_options)
{
  "program": "blastn",
  "db": "core_nt",
  "query_fasta": ">query1\nACGT...",
  "external_correlation_id": "<hex>",
  "blast_options": { "outfmt": "7 std staxids sstrand qseq sseq" }
}
```

A body carrying `blast_options` is routed to `POST /v1/jobs`; otherwise it goes
to the XML-locked `/api/v1/elastic-blast/submit`.

## Configuration

| Env var | Default |
| --- | --- |
| `SERVICEBUS_NAMESPACE_FQDN` | `sb-elb-dashboard-krc.servicebus.windows.net` |
| `SERVICEBUS_REQUEST_QUEUE` | `elastic-blast-requests` |

## Auth

All scripts authenticate with `DefaultAzureCredential` (interactive `az login`
or a managed identity). Required roles on the namespace:

* `send_request.py` → **Azure Service Bus Data Sender**
* `consume.py` → **Azure Service Bus Data Receiver**
* `monitor.py` → **Azure Service Bus Data Owner** (counts), or
  **Data Receiver** with `--peek-only`

## Quick start

```bash
pip install azure-servicebus azure-identity

# Build + print a request without sending (no Azure needed):
python send_request.py --dry-run
python send_request.py --mode v1 --dry-run

# Send one request (needs az login + Sender role):
python send_request.py --db core_nt --program blastn

# Watch the queue and drain it:
python monitor.py
python consume.py --max 5
```
