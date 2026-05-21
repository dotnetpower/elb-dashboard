# API Reference

The API Reference page is for developers and platform maintainers who need to inspect or test the ElasticBLAST OpenAPI surface directly. Most researchers should keep using the Dashboard, New Search, Jobs, and Results pages for day-to-day work.

![API Reference page with endpoint groups and API menu selected](../images/screenshots/api-reference.png)

## When To Use It

Use the API Reference when you need to:

- Confirm which endpoints are available in the deployed OpenAPI service.
- Check request and response shapes before wiring an external workflow.
- Run a safe read-only `Try` request such as health, config, cluster status, or job listing.
- Open Swagger UI for a fuller OpenAPI explorer.

For submitting production BLAST searches, prefer the New Search page unless you are validating an integration path.

## Finding Endpoints

The left sidebar groups endpoints by method and tag. Use it to move quickly between system checks, cluster status, job submission, job monitoring, and result download routes.

The main panel shows each endpoint as a compact row with:

- HTTP method and path.
- A short operation summary.
- A `Try` action when the route can be exercised from the browser.
- A disclosure control for request and response details.

Expanded response cards show the response shape, next action, key fields, and a
JSON example when the OpenAPI document publishes a schema or when the dashboard
provides a curated example for high-value routes such as `GET /v1/cluster`. If a
route does not publish a response schema, the card says so instead of showing an
empty success panel.

## Authentication

The API token panel shows whether the `X-ELB-API-Token` value is configured for the sibling OpenAPI service. External clients must send this token in the request header when calling the OpenAPI endpoint directly.

![API token panel showing the Copy action](../images/screenshots/api-token-panel.svg)

Use **Copy** in the token panel, then add the copied value as an HTTP header:

```http
X-ELB-API-Token: <copied-token>
```

For example:

```bash
curl -H "X-ELB-API-Token: <copied-token>" \
	"https://api.example.internal/v1/jobs"
```

The API Reference page's `Try` buttons use the same token internally. When you click `Try` from the browser, the dashboard forwards the request with the configured `X-ELB-API-Token`; you do not need to paste the token into the `Try` request manually.

Keep the token hidden in screenshots, demos, and shared notes. Regenerate it only when rotating integration credentials or recovering from a suspected exposure.

The dashboard itself still uses the signed-in Azure identity for access. The OpenAPI token is for calls forwarded to the AKS-hosted OpenAPI execution service.

## Submit A BLAST Job

To submit a BLAST job from the API Reference page, expand `POST /v1/jobs`, choose the inline FASTA request body, paste the sequence into `query_fasta`, and click **Send Request**.

![POST /v1/jobs with query_fasta and a job_id response](../images/screenshots/api-job-submit-flow.svg)

The inline FASTA request below searches `core_nt` with the `NC_003310.1` example. Keep the FASTA as a single JSON string with `\n` between FASTA lines.

```json
{
  "program": "blastn",
  "db": "core_nt",
  "query_fasta": ">NC_003310.1:c48509-48048 Monkeypox virus, complete genome\nATGGAGAAGCGAGAAGTTAATAAAGCTCTGTATGATCTTCAACGTAGTACTATGGTGTACAGTTCCGACG\nATACTCCTCCTCGTTGGTCTACGACAATGGATGCTGATACACGGCCTACAGATTCTGATGCTGATGCTAT\nAATAGATGATGTATCCCGCGAAAAATCAATGAGAGAGGATAATAAGTCTTTTGATGATGTTATTCCGGTT\nAAAAAAATTATTTATTGGAAAGGTGTTAACCCTGTCACCGTTATTAATGAGTACTGCCAAATAACTAGGA\nGAGATTGGTCTTTTCGTATTGAATCAGTGGGGCCTAGTAACTCTCCTACATTTTATGCCTGTGTAGACAT\nTGACGGAAGAGTATTCGATAAGGCAGATGGAAAATCTAAACGAGATGCTAAAAATAATGCAGCTAAATTG\nGCTGTAGATAAACTTCTTAGTTATGTCATCATTAGATTCTGA\n",
  "blast_options": {
    "extra": "-evalue 0.05 -word_size 28 -max_target_seqs 100 -outfmt 5 -dust yes -soft_masking false -searchsp 32156241807668"
  }
}
```

That request maps to this BLAST command shape after the service resolves storage paths:

```bash
blastn -db core_nt -evalue 0.05 -word_size 28 -max_target_seqs 100 -outfmt 5 -dust yes -soft_masking false -searchsp 32156241807668 -query query.fasta -out results.out
```

A successful submission returns `202` with a `job_id`. In the OpenAPI surface,
this top-level `job_id` is the short OpenAPI job id. Copy that value for status
polling; do not use the Dashboard UUID from a `/blast/jobs/<uuid>` page URL.

```json
{
  "job_id": "17dfd2825089",
  "status": "dispatching"
}
```

## Response Contract

The OpenAPI service and the dashboard wrapper both treat BLAST submission as asynchronous work. A `2xx` response means the request was accepted or a current state was returned; it does not mean the BLAST run has completed successfully.

Dashboard API responses keep their existing top-level fields and add four compatibility fields for new clients:

- `operation`: control-plane work to poll, including `operation_id`, `state`, `poll_after_seconds`, and links such as `/api/operations/{operation_id}`.
- `target`: the BLAST job resource identity. It separates the Dashboard UUID from the short OpenAPI `job_id`.
- `admission`: the point-in-time queue and capacity decision. Treat this as a snapshot, not a completion guarantee.
- `meta`: request correlation data such as `request_id` for logs and support.

Use the right identifier for the endpoint you are calling. The status endpoint
is named `GET /v1/jobs/{job_id}/status` for OpenAPI compatibility, but that path
parameter means **OpenAPI job id**, not Dashboard job UUID.

| Identifier         | Example                                | Use with                                          |
| ------------------ | -------------------------------------- | ------------------------------------------------- |
| OpenAPI job id     | `17dfd2825089`                         | `/v1/jobs/{job_id}/status`                        |
| Dashboard job UUID | `bb61858a-8cb6-4590-a2e3-c144662851f7` | `/blast/jobs/<uuid>` and `/api/blast/jobs/{uuid}` |

The API Reference page labels the status path parameter as **OpenAPI job id** and
shows a `job_id = OpenAPI id` hint next to job-scoped status routes.

```json
{
  "job_id": "bb61858a-8cb6-4590-a2e3-c144662851f7",
  "job_id_kind": "dashboard",
  "status": "queued",
  "operation_status_url": "/api/operations/task-123",
  "operation": {
    "operation_id": "task-123",
    "operation_type": "blast.submit",
    "state": "queued",
    "poll_after_seconds": 5
  },
  "target": {
    "resource_type": "blast_job",
    "job_id_kind": "dashboard",
    "dashboard_job_id": "bb61858a-8cb6-4590-a2e3-c144662851f7",
    "openapi_job_id": "17dfd2825089"
  },
  "admission": {
    "decision": "accepted",
    "reason": "queued_for_blast_execution"
  },
  "meta": {
    "request_id": "01HX7V8W4D9Y3F9PZQ2QK4N7RA"
  }
}
```

For an external client, send the same request with the API token header:

```bash
curl -X POST "https://api.example.internal/v1/jobs" \
	-H "Content-Type: application/json" \
	-H "X-ELB-API-Token: <copied-token>" \
	--data-raw '{"program":"blastn","db":"core_nt","query_fasta":">NC_003310.1:c48509-48048 Monkeypox virus, complete genome\nATGGAGAAGCGAGAAGTTAATAAAGCTCTGTATGATCTTCAACGTAGTACTATGGTGTACAGTTCCGACG\nATACTCCTCCTCGTTGGTCTACGACAATGGATGCTGATACACGGCCTACAGATTCTGATGCTGATGCTAT\nAATAGATGATGTATCCCGCGAAAAATCAATGAGAGAGGATAATAAGTCTTTTGATGATGTTATTCCGGTT\nAAAAAAATTATTTATTGGAAAGGTGTTAACCCTGTCACCGTTATTAATGAGTACTGCCAAATAACTAGGA\nGAGATTGGTCTTTTCGTATTGAATCAGTGGGGCCTAGTAACTCTCCTACATTTTATGCCTGTGTAGACAT\nTGACGGAAGAGTATTCGATAAGGCAGATGGAAAATCTAAACGAGATGCTAAAAATAATGCAGCTAAATTG\nGCTGTAGATAAACTTCTTAGTTATGTCATCATTAGATTCTGA\n","blast_options":{"extra":"-evalue 0.05 -word_size 28 -max_target_seqs 100 -outfmt 5 -dust yes -soft_masking false -searchsp 32156241807668"}}'
```

## Check Job Status

After copying the OpenAPI job id, expand `GET /v1/jobs/{job_id}/status`, paste
the value into the **OpenAPI job id** path parameter, and click **Send Request**.

![GET /v1/jobs/{job_id}/status with a running job response](../images/screenshots/api-job-status-flow.svg)

The response shows the current job lifecycle state. Early responses commonly move from `dispatching` to `running`; completed jobs include the result metadata needed by downstream result routes.

```json
{
  "job_id": "17dfd2825089",
  "status": "running",
  "phase": "submitting",
  "program": "blastn",
  "db": "core_nt"
}
```

For an external client, call the status endpoint with the copied OpenAPI job id
and the same token header:

```bash
curl -H "X-ELB-API-Token: <copied-token>" \
	"https://api.example.internal/v1/jobs/17dfd2825089/status"
```

## Safe Screenshot Practice

Before publishing API Reference screenshots, make sure the page does not expose subscription IDs, tenant-specific hostnames, raw API tokens, or private resource names. The screenshot above uses a masked example API endpoint and does not reveal a token value.
