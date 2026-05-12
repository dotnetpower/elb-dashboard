# DNS label uniqueness across resource groups

**Date**: 2026-05-12
**Scope**: `api/services/network.py`

## Motivation

Even after making `ensure_network` GET-reuse the existing PIP within
the same RG, provisioning a Remote Terminal in a *different* RG with
the default VM name still failed:

```
HttpResponseError: (DnsRecordInUse) DNS record
elb-term-vm-elb-terminal.koreacentral.cloudapp.azure.com is already
used by another public IP.
```

Azure DNS labels under `*.<region>.cloudapp.azure.com` are unique per
region across the entire cloud, so once `rg-elb-terminal/pip-vm-elb-terminal`
holds `elb-term-vm-elb-terminal`, no other RG can use the same label.

## User-facing change

Multiple resource groups can now provision their own Remote Terminal
with the default VM name `vm-elb-terminal` without colliding. Each
RG/sub combination gets its own FQDN.

The FQDN now looks like:

```
elb-term-vm-elb-terminal-66a4dd.koreacentral.cloudapp.azure.com
```

(short stable hash suffix at the end).

## API / IaC diff summary

`api/services/network.py`:

- New `_dns_label(subscription_id, resource_group, vm_name)` helper:
  takes the sanitised VM name and appends a 6-character SHA-256 hex
  suffix derived from the `(sub, rg, vm)` tuple. Result is truncated
  to 63 chars and stripped of trailing dashes to satisfy DNS label
  rules.
- `ensure_network` calls the helper instead of the hard-coded
  `f"elb-term-{vm_name.lower()}"`.
- The label is included in the log line so operators can correlate
  PIPs to provisioning calls.

This is **stable** (same input → same label) so re-running the wizard
against the same RG still hits the GET-reuse path; only different RGs
diverge.

## Validation evidence

- Local sanity check: two different RGs → two different labels, both
  31 chars, both start with the expected prefix.
- `pytest -q api/tests/` → 13 passed.
- API redeployed via `WEBSITE_RUN_FROM_PACKAGE` user-delegation SAS
  (`funcapp-dnshash.zip`), Function App restarted.
- Pending: user retries Provision Terminal in `rg-elb-demo-terminal`
  to confirm the orchestrator passes the Network step.
