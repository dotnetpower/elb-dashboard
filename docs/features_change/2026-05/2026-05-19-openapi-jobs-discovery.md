# OpenAPI jobs discovery

## Motivation

The BLAST Jobs page could show an empty list when Azure Table job state was unavailable and the API sidecar did not have `ELB_OPENAPI_BASE_URL` configured. In that state, jobs submitted through the AKS `elb-openapi` service were still running, but `/api/blast/jobs` had no way to discover the sibling service endpoint.

## User-facing change

The Jobs page now passes the selected subscription, workload resource group, and AKS cluster name to `/api/blast/jobs`. The backend uses that context to discover the in-cluster `elb-openapi` LoadBalancer endpoint and server-side API token, then merges external OpenAPI jobs into the canonical jobs response.

## API / runtime diff summary

- `blastApi.listJobs` accepts optional workspace context query parameters.
- `/api/blast/jobs` accepts `subscription_id`, `resource_group`, and `cluster_name` and uses them to build an OpenAPI client context.
- `api.services.external_blast` can receive per-call `base_url` and `api_token` values instead of relying only on process environment variables.
- `api.services.k8s_monitoring` can read a literal Deployment env value for server-side OpenAPI authentication. The value is never returned to the browser.
- External OpenAPI `completed` statuses now map to dashboard `completed` instead of falling back to `running`.

## Validation evidence

- `uv run ruff format api/services/external_blast.py api/services/k8s_monitoring.py api/routes/stubs.py api/tests/test_external_blast_api.py`
- `uv run ruff check api/services/external_blast.py api/services/k8s_monitoring.py api/routes/stubs.py api/tests/test_external_blast_api.py`
- `uv run pytest -q api/tests/test_external_blast_api.py`
- `npm run build` in `web/`