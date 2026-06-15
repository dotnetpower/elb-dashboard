---
title: Custom domain for the OpenAPI public HTTPS endpoint
description: Expose the in-cluster elb-openapi service on an operator domain (e.g. api.elasticblast.com) with an auto-created Azure DNS record and a Let's Encrypt certificate issued for the custom domain.
tags:
  - operate
  - blast
---

# Custom domain for the OpenAPI public HTTPS endpoint

## Motivation

The public HTTPS path in front of the in-cluster `elb-openapi` service was hard-pinned
to the Azure-assigned `*.cloudapp.azure.com` FQDN. An operator who owns a domain
(e.g. an [Azure DNS](https://learn.microsoft.com/azure/dns/dns-overview) zone
`elasticblast.com`) wants the API served on a stable, branded host such as
`api.elasticblast.com`, with the [Let's Encrypt](https://letsencrypt.org/)
certificate issued for that name.

## User-facing change

**Settings → Public HTTPS** now has an optional **Custom domain** field:

- Leave it empty → unchanged behaviour (cloudapp FQDN + cert for that FQDN).
- Set `api.elasticblast.com` → the setup pipeline:
  1. keeps the cloudapp FQDN as the stable CNAME target,
  2. best-effort upserts a DNS record in the owning Azure DNS zone
     (CNAME → cloudapp FQDN for a sub-domain; A → LB IP for an apex),
  3. applies the Ingress + Certificate with the custom domain as the host,
  4. cert-manager runs the [HTTP-01](https://cert-manager.io/docs/) challenge against
     the custom domain,
  5. stores `https://api.elasticblast.com` as the public base URL.

If the managed identity lacks DNS permission, or no hosted zone owns the domain, the
DNS step **degrades to a manual instruction** (returned in the task result) instead of
aborting — the certificate still issues once the operator adds the record.

Client- and server-side validation gate the field to a bare public-TLD FQDN (no
scheme/path/port; private-use TLDs rejected, same as the operator email).

## API / IaC diff summary

- **Dependency**: `azure-mgmt-dns==8.2.0` added.
- **Service**: `api/services/azure_clients.py::dns_client`; new
  `api/services/azure_dns.py` (`split_custom_domain`, `find_zone_for_fqdn`,
  `ensure_public_dns_record`) — best-effort, never raises.
- **Pipeline**: `setup_openapi_public_https` gains a `custom_domain` arg; the effective
  Ingress/cert host becomes the custom domain while the cloudapp FQDN stays the CNAME
  target. New `ensure_custom_domain_dns` progress phase. Metadata + result carry
  `custom_domain`, `cloudapp_fqdn`, `dns_record`.
- **Route**: `OpenApiPublicHttpsRequest.custom_domain` + `_validate_custom_domain`.
- **SPA**: custom domain input in `PublicHttpsSection`, `isValidCustomDomain` helper,
  `enableOpenApiPublicHttps(..., customDomain)`.
- **IaC**: new `infra/modules/dnsZoneRoles.bicep` (DNS Zone Contributor, zone-scoped)
  wired into `infra/main.bicep` as a conditional module gated on
  `openApiCustomDnsZoneName` (default empty = no new role — charter §12a Rule 4).

## RBAC (apply separately — not auto-deployed by this change)

The DNS auto-creation needs the shared user-assigned managed identity to hold
**DNS Zone Contributor** on the hosted zone. This change ships the Bicep but does
**not** redeploy. To enable automation, the maintainer sets the params and provisions:

```bash
azd provision \
  -e <env> \
  --set openApiCustomDnsZoneName=elasticblast.com \
  --set openApiCustomDnsZoneResourceGroup=rg-elb-dashboard
```

Or grant it directly (least privilege, zone-scoped):

```bash
ZONE_ID=$(az network dns zone show -g rg-elb-dashboard -n elasticblast.com --query id -o tsv)
az role assignment create \
  --assignee <shared-uami-principal-id> \
  --role "DNS Zone Contributor" \
  --scope "$ZONE_ID"
```

Until then the pipeline still works — it just returns the manual "create this record"
instruction instead of writing the CNAME itself.

## Validation evidence

- `uv run pytest -q api/tests` → 3810 passed, 3 skipped (new
  `api/tests/test_azure_dns.py`: split / longest-suffix zone match / apex / CNAME
  upsert / no-zone / forbidden degrade; `test_route_validate_custom_domain`).
- `uv run ruff check` clean on all touched Python files.
- `az bicep build --file infra/main.bicep` compiles clean.
- `cd web && npm run build` clean; `npm test -- --run` → 898 passed; ESLint clean.

## Live validation (cannot be done locally)

Actual certificate issuance requires a real DNS resolution + Let's Encrypt order, so
verify against a live cluster after deploying:

1. Enable Public HTTPS with `custom_domain=api.elasticblast.com`.
2. Confirm the CNAME exists: `az network dns record-set cname show -g rg-elb-dashboard -z elasticblast.com -n api`.
3. Wait for the cert: the task's `wait_certificate_ready` phase turns green (1-3 min first issuance).
4. `curl https://api.elasticblast.com/openapi.json -H "X-ELB-API-Token: <token>"`.
