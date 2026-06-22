---
title: Service Bus result downloads — pre-authorize the Azure CLI
description: Why the Service Bus completion-event download_url needs a bearer token, the AADSTS65001 failure it causes, and how an Entra admin runs preauthorize-cli-app.sh to make the documented consumer download path work.
tags:
  - operate
  - auth
  - blast
---

# Service Bus result downloads — pre-authorize the Azure CLI

A consumer of the [`elastic-blast-completions`](../architecture/service-bus-integration.md)
topic downloads result files by calling each
`result_files[].download_url` from a `succeeded` event. That URL points at the
dashboard's **authenticated streaming gateway**
(`GET /api/v1/elastic-blast/jobs/{id}/files/{file_id}`) — never a
[SAS](https://learn.microsoft.com/azure/storage/common/storage-sas-overview)
token — because the workload [Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-account-overview)
account is `publicNetworkAccess: Disabled` and only the Container App can reach
it over a private endpoint. The gateway therefore requires a bearer token for
`aud=api://<API_CLIENT_ID>`.

## Symptom: download returns 401 (`AADSTS65001`)

The shipped example [`example/servicebus/consume.py`](https://github.com/dotnetpower/elb-dashboard/blob/main/example/servicebus/consume.py)
`--download` acquires that token with:

```bash
az account get-access-token --resource <api-client-id>
```

On a fresh deployment this fails with `AADSTS65001` ("the user or administrator
has not consented"), the script gets an empty token, and the download endpoint
returns `401 missing bearer token`. The cause is purely
[Microsoft Entra](https://learn.microsoft.com/entra/identity/) app-registration
configuration: the API app exposes the `user_impersonation` delegated scope but
has not **pre-authorized** the well-known
[Azure CLI](https://learn.microsoft.com/cli/azure/what-is-azure-cli) public
client (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) for it, so a non-interactive
token request cannot satisfy the consent prompt.

!!! note "This is not a control-plane bug"
    The download route, token self-heal, and the Storage fallback are all
    healthy — an unauthenticated call correctly returns `401`. No container
    image change is involved, so this is **not** fixed by a redeploy. The fix is
    a one-time app-registration change.

## Fix: run `preauthorize-cli-app.sh`

[`scripts/dev/preauthorize-cli-app.sh`](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/preauthorize-cli-app.sh)
is a standalone, idempotent tool that pre-authorizes the Azure CLI public client
for the app's `user_impersonation` scope — and touches nothing else.

!!! warning "Who runs this"
    Patching an app registration needs **`Application.ReadWrite.All`**
    (Application Administrator / Cloud Application Administrator) or ownership of
    the app. A developer without that role hands this script to an Entra admin.
    The script takes the API client id as an argument, so no tenant/app
    identifiers are baked into the repository.

### 1. Find the API client id

```bash
az containerapp show -n ca-elb-dashboard -g <resource-group> \
  --query "properties.template.containers[?name=='api'][].env[?name=='API_CLIENT_ID'][].value" -o tsv
```

### 2. Preview the change (no mutation)

The admin signs in (`az login --tenant <tenant-id>`), then dry-runs to review the
exact Microsoft Graph PATCH body before applying anything:

```bash
./scripts/dev/preauthorize-cli-app.sh <api-client-id> <tenant-id> --dry-run
```

### 3. Apply

```bash
./scripts/dev/preauthorize-cli-app.sh <api-client-id> <tenant-id>
```

The script is idempotent (a second run reports "already pre-authorized"),
asserts the active `az` tenant matches, reconstructs the `user_impersonation`
scope if the read-back is stale so it can never wipe it, verifies the grant
after patching, and prints a clear error if the runner lacks write access.

### 4. Confirm the consumer can download

```bash
# token acquisition now succeeds without a consent prompt
az account get-access-token --resource <api-client-id> --query expiresOn -o tsv

# end-to-end: receive completion events and download result files
ELB_API_CLIENT_ID=<api-client-id> \
  python example/servicebus/consume.py --source completions --download --download-dir ./out
```

!!! info "Security note"
    Pre-authorizing the Azure CLI only removes the **consent prompt** for a scope
    the dashboard SPA already requests; it grants no data access those tenant
    users do not already have. The backend still validates every token
    (`require_caller`) on each call.

## Alternatives

- **Already have a token?** Set `ELB_BEARER_TOKEN` directly and the example skips
  the `az` acquisition entirely.
- **Fresh deployments** configured by
  [`scripts/dev/setup-app-registration.sh`](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/setup-app-registration.sh)
  already include this pre-authorization, so this manual step is only needed for
  an app registration created before that change (or created another way).

See also: [Service Bus Examples](../architecture/service-bus-examples.md) and the
[Service Bus BLAST Integration](../architecture/service-bus-integration.md)
architecture pages.
