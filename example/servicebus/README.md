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

## Completion topic — download_url contract (external consumers)

A succeeded job lands on the **completion topic** `elastic-blast-completions`
(default subscription `default`) as a `blast.transition` event:

```jsonc
{
  "event_id": "<deterministic hash, dedup key>",
  "external_correlation_id": "<the producer's id>",
  "openapi_job_id": "<12-hex>",
  "status": "succeeded",
  "request_id": "<producer-supplied tracking value>",
  "result_files": [
    {
      "file_id": "result-001",
      "name": "batch_000-blastn-core_nt_shard_00.out.gz",
      "format": "blast_xml",          // or "blast_tabular" for outfmt 7
      "size": 11295,
      "download_url": "https://<dashboard-fqdn>/api/v1/elastic-blast/jobs/<job>/files/result-001"
    }
  ]
}
```

The `download_url` points at the dashboard authenticated gateway (NEVER a
SAS URL — charter §9 holds). To download:

```python
# bearer for the API audience (resource = $ELB_API_CLIENT_ID)
import urllib.request, os
req = urllib.request.Request(
    file["download_url"],
    headers={"Authorization": f"Bearer {bearer}"},
)
with urllib.request.urlopen(req, timeout=60) as r:
    open(file["name"], "wb").write(r.read())
```

See [`consume.py`](consume.py) `--source completions --download` for a working
end-to-end consumer (bearer via `ELB_BEARER_TOKEN` or
`az account get-access-token --resource $ELB_API_CLIENT_ID`).

### Operational guarantees for external consumers

| Concern | Guarantee |
| --- | --- |
| Outage tolerance | Service Bus Standard topic retention = **14 days**. A consumer that stays disconnected for ≤14 days catches up on reconnect without loss. Longer → tail messages roll off. |
| At-least-once | Both the request queue and the completion topic are at-least-once. The producer side dedups via `external_correlation_id` (`claim_bridge` atomic gate). Consumers MUST be idempotent on `event_id` (the deterministic hash) — see `consume.py` for the pattern. |
| Out-of-order | Transitions are NOT strictly ordered (`queued` / `running` / `succeeded` can arrive in any order on a fast cluster). Trust `status` + `event_id`, not delivery order. |
| Retry on `download_url` | A transient 401 self-heals once via internal token resync (the gateway reopens the upstream stream). A 5xx from the gateway is also transient — retry with exponential backoff (2s, 8s, 30s). A 404 means the file is gone — stop retrying. |
| `download_url` validity | The URL itself is durable (gateway path); the access token in the `Authorization` header is the only expiring piece. Re-mint the bearer just before each session. |
| DLQ on the topic subscription | The dashboard does NOT consume the `default` subscription; if the external consumer falls behind, messages accumulate (and eventually DLQ after default delivery-count exhaustion under your subscription's policy). Operators monitor this via the Message Flow card. |

### Recommended consumer skeleton

```python
seen = set()              # event_id de-dup (e.g. Redis SET in production)
while True:
    msg = receiver.receive_messages(max_count=1, max_wait_time=30)
    if not msg:
        continue
    body = json.loads(msg[0].body)
    if body["event_id"] in seen:
        receiver.complete_message(msg[0])
        continue
    try:
        for f in body.get("result_files", []):
            download_with_retry(f["download_url"], bearer)
        seen.add(body["event_id"])
        receiver.complete_message(msg[0])
    except TransientError:
        receiver.abandon_message(msg[0])
    except PermanentError:
        receiver.dead_letter_message(msg[0], reason="...")
```
