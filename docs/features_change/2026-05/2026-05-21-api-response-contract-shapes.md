# 2026-05-21 — API Reference response contract shapes

## Motivation

The [OpenAPI](https://spec.openapis.org/oas/latest.html) Reference previously emphasized HTTP status codes more than response
object contracts. That made async BLAST workflows harder to document because a
`202` response means the request was accepted, not that BLAST completed, and
operators still need to know which IDs to persist and where to poll next.

## User-facing change

The API Reference now summarizes endpoint responses by response shape, such as
`202 JobSubmitAccepted`, `4xx ErrorResponse`, and `5xx RuntimeFailure`. Expanded
endpoint cards show status-specific response examples, key fields, and the next
action a caller should take.

The global API response contract panel remains available as an overview of
`operation`, `target`, `admission`, and `meta`, but it is collapsed by default so
the endpoint cards stay scannable.

Job-scoped status endpoints now call out that `{job_id}` means the short OpenAPI
job id returned by `POST /v1/jobs`, not the Dashboard UUID from `/blast/jobs/<uuid>`.
Generic endpoint responses now use published OpenAPI JSON schemas and examples
when available, and `GET /v1/cluster` has a curated `ClusterOverview` example so
its `200` response is not an empty success shell.

The dashboard proxy also rejects Dashboard UUIDs before forwarding job-scoped
OpenAPI paths such as `/v1/jobs/{job_id}/status`, `/v1/jobs/{job_id}/results`,
or `DELETE /v1/jobs/{job_id}`. That keeps API Reference retries from reaching
the AKS-hosted OpenAPI service with an identifier that only belongs to the
dashboard control plane.

## API and UI diff summary

| Area                         | Change                                                                                                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/v1/jobs` request example   | Keeps the `core_nt` / `NC_003310.1` inline FASTA example as the default Mode B payload with `outfmt: 5`.                                                            |
| Endpoint response metadata   | Adds curated `shapeName`, `nextAction`, key field, and JSON example metadata for common success, queue rejection, validation, not found, and runtime failure cases. |
| Schema-backed response cards | Derives key fields and sample JSON from OpenAPI response schemas when an endpoint does not have a curated response contract.                                        |
| Cluster overview response    | Adds a concrete `ClusterOverview` `200` example with `cluster_name`, `nodes`, `pods`, and `pod_summary`.                                                            |
| Endpoint card header         | Replaces raw status-code lists with representative shape labels.                                                                                                    |
| Job status parameter hints   | Labels `{job_id}` as the OpenAPI job id, adds the example value, and shows where the Dashboard UUID should be used instead.                                         |
| OpenAPI proxy guard          | Rejects Dashboard UUIDs on job-scoped OpenAPI paths with `dashboard_job_id_not_openapi_job_id` before forwarding the request.                                     |
| Expanded `Responses` section | Renders shape cards with the HTTP code, contract name, next step, important fields, and highlighted JSON example.                                                   |
| Documentation                | Updates the user guide to explain async response fields and the OpenAPI job ID versus Dashboard UUID distinction.                                                   |

## Validation evidence

- `cd web && npm run test -- src/pages/apiReference/spec.test.ts`
- `cd web && npx prettier --check src/pages/apiReference/spec.ts src/pages/apiReference/spec.test.ts src/pages/apiReference/EndpointCard.tsx src/pages/apiReference/types.ts`
- `cd web && npm run lint -- --quiet`
- `cd web && npm run build`
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_openapi_proxy_route.py api/tests/test_route_contracts.py api/tests/test_response_contracts.py`
- `uv run ruff check api/routes/aks/openapi.py api/tests/test_openapi_proxy_route.py`
- `curl -i 'http://127.0.0.1:8085/api/aks/openapi/proxy?resource_group=rg-elb-01&cluster_name=elb-cluster&path=%2Fv1%2Fjobs%2F9b45dbfe-1c63-433e-a650-609e2d43bbd8%2Fresults'` returned `400 dashboard_job_id_not_openapi_job_id`.
- `curl -i 'http://127.0.0.1:8085/api/aks/openapi/proxy?resource_group=rg-elb-01&cluster_name=elb-cluster&path=%2Fhealthz'` returned `200 {"status":"ok"}`.
- `uv run mkdocs build`
- Browser smoke with a mocked OpenAPI spec confirmed `/v1/jobs` displays `202 JobSubmitAccepted`, `4xx ErrorResponse`, `5xx RuntimeFailure`, and an expanded response card with `Next:`, key fields, and JSON including `operation_id` and `admission`.
- Browser smoke with a mocked OpenAPI spec confirmed `GET /v1/jobs/{job_id}/status` displays `job_id = OpenAPI id`, labels the path input as `OpenAPI job id`, and shows separate usage examples for the OpenAPI job id and Dashboard job UUID.
- Browser smoke with a mocked OpenAPI spec confirmed `GET /v1/cluster` displays `200 ClusterOverview` with `nodes`, `pods`, and `pod_summary` fields plus JSON, and that schema-backed endpoints such as `/v1/runtime` derive fields and JSON from component schemas.
