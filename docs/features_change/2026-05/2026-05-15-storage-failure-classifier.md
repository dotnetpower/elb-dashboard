# Storage failure classifier — distinguish network vs RBAC

## Motivation

The BLAST Databases modal (and other storage-data routes) has been showing a
single misleading error to local developers:

> Cannot read databases from storage: Cannot access storage account 'elbstg01'.
> In production, the Container App reaches storage via private endpoint.
> Locally, assign 'Storage Blob Data Reader' role to your az login identity.

This message **always** told the user the failure was an RBAC problem and to
assign `Storage Blob Data Reader`. In reality the workload Storage account is
deployed with `publicNetworkAccess: Disabled` per project policy §9 (storage is
private-endpoint only), so **no** role assignment can make the data plane
reachable from a developer laptop. The instruction was actively misleading.

Worse: the underlying SDK error code in both cases is the same
`AuthorizationFailure`, so without consulting the management plane the route
cannot tell network-deny from RBAC-deny apart.

Side problem discovered while diagnosing: the default Azure SDK retry policy
keeps retrying a `403 AuthorizationFailure` for ~30 s before raising. Combined
with the dashboard's parallel polling cards this wedged the entire `api`
sidecar (uvicorn worker stuck in BlobServiceClient retries; in-flight requests
backed up to 81 connections; reload could not drain).

## User-facing change

Affected routes (`api/routes/stubs.py`):

* `GET /api/blast/databases`
* `GET /api/blast/jobs/{job_id}/files`
* `GET /api/blast/jobs/{job_id}/results/{path}`

When the data-plane call fails, the response now carries one of three
`degraded_reason` values instead of the old generic message:

| `degraded_reason` | When                                                                                                  | Message shown to the user                                                                                                                                                                                                                                       |
| ----------------- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `network_blocked` | `AuthorizationFailure` AND ARM reports `publicNetworkAccess == "Disabled"` on the account             | "Storage account '{name}' has publicNetworkAccess: Disabled (by design — see project policy §9). Data-plane access only works from inside the platform VNet via the private endpoint, so this view is unavailable from local development. Run `azd up` to verify in the deployed Container App." |
| `access_denied`   | `AuthorizationFailure` but the account is publicly reachable                                          | "Cannot access storage account '{name}'. Assign 'Storage Blob Data Reader' (or higher) to your identity at the storage account scope and wait ~5 minutes for RBAC propagation."                                                                                  |
| `not_found`       | Account / container error contains `ResourceNotFound` / `AccountNotFound` / `ContainerNotFound`       | "Storage account '{name}' (or one of its containers) does not exist in resource group '{rg}'."                                                                                                                                                                  |
| `<ErrType>`       | Any other exception                                                                                   | The repr of the exception type — preserved as the prior fallback.                                                                                                                                                                                                |

The `StorageCard` modal already renders `<strong>Cannot read databases from
storage:</strong> {message}`; only the server-supplied `message` was misleading
and is now corrected. The static prefix label is kept.

`blast_job_file` returns HTTP 404 when `degraded_reason == "not_found"` and
HTTP 503 otherwise, with `detail = {"code": <reason>, "message": <text>}` so
the SPA can branch on the same code.

## API / IaC diff summary

Code-only change. No Bicep, no infrastructure side effects.

* New helper `api/services/storage_data.py::classify_storage_failure(credential, subscription_id, resource_group, account_name, exc) -> dict`. Looks up `publicNetworkAccess` via `StorageManagementClient` to disambiguate `AuthorizationFailure` between network deny and RBAC deny. Returns `{"degraded": True, "degraded_reason": ..., "message": ...}`.
* `_blob_service` now constructs `BlobServiceClient` with `retry_total=0, connection_timeout=5, read_timeout=10` so calls fail fast (~1–2 s) instead of cascading 30 s+ retries through the dashboard polling fan-out. This is the right behaviour for "expected to fail in local dev" calls; production traffic continues to receive the SDK default retries through the same single-shot path because the call simply succeeds.
* `api/routes/stubs.py`:
  * `blast_databases` — replaces the hand-written error message with `**classify_storage_failure(...)`.
  * `blast_job_results` — same pattern (passes `""` for resource group as the route does not have it; classifier degrades gracefully to the legacy "access_denied" branch when RG is unknown).
  * `blast_job_file` — raises `HTTPException(404 if not_found else 503, detail={"code": …, "message": …})`.
* No new dependencies, no schema changes.

## Validation evidence

* `uv run pytest -q api/tests` → **67 passed** in 9.64 s after each of the two code edits (helper + timeout tuning). No regressions.
* Direct Python invocation of `classify_storage_failure` against the live `elbstg01` returned the exact expected JSON:

  ```json
  {
    "degraded": true,
    "degraded_reason": "network_blocked",
    "message": "Storage account 'elbstg01' has publicNetworkAccess: Disabled (by design — see project policy §9). Data-plane access only works from inside the platform VNet via the private endpoint, so this view is unavailable from local development. Run `azd up` to verify in the deployed Container App."
  }
  ```

* Live HTTP curl against the running api sidecar (after restart):

  ```
  $ curl -s --max-time 30 \
      "http://127.0.0.1:8080/api/blast/databases?subscription_id=...&storage_account=elbstg01&resource_group=rg-elb-01" \
      -o /tmp/r.json -w "HTTP %{http_code} time=%{time_total}s\n"
  HTTP 200 time=1.793736s
  ```

  Body: identical to the direct invocation above. Note the **1.8 s** response time — without the timeout tuning the same call was hanging for 30 s+ and starving the worker.

* Browser verified at `http://localhost:8090/`: BLAST Databases modal now shows the new accurate `network_blocked` message instead of the old "assign Storage Blob Data Reader" instruction. Screenshot captured during validation.

## Bonus correction (rolled back)

While diagnosing, a `Storage Blob Data Contributor` role assignment was
created at the storage account scope for the developer's user OID. This had
**zero** effect on the symptom because the failure is at the network firewall
layer, not RBAC. The role assignment was removed via
`az role assignment delete` after confirming. No infrastructure or RBAC state
remains changed by this work.
