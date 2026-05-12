# Idempotent Public IP reuse in ensure_network

**Date**: 2026-05-12
**Scope**: `api/services/network.py`

## Motivation

Re-running the Provision Terminal wizard against an existing setup
failed at step 2 (Network) with:

```
HttpResponseError: (DnsRecordInUse) DNS record
elb-term-vm-elb-terminal.koreacentral.cloudapp.azure.com is already
used by another public IP.
```

The PIP exists in `rg-elb-terminal` with the exact name, location, and
DNS label the orchestrator wants to PUT, so a re-PUT should be a no-op.
Azure's networking control plane returns `DnsRecordInUse` for some
SKU/region combinations when the same DNS label appears in the
incoming PUT body even though the existing PIP holds it — i.e. the
request is interpreted as a new DNS reservation conflicting with
itself. The orchestration could not progress past step 2 even though
the underlying resources were already in the desired state.

## User-facing change

Re-clicking "Provision" in the wizard when a Remote Terminal already
exists no longer fails at the Network step. The orchestrator advances
through Key Vault, password rotation, VM update, and cloud-init
verification, returning the live connection info.

## API / IaC diff summary

`api/services/network.py` `ensure_network`:

- Before issuing `begin_create_or_update` on the public IP, attempt a
  `GET` against the resource group.
- If the existing PIP has the same `domain_name_label` and `location`
  as the request, reuse it directly (skip the PUT).
- Only on `ResourceNotFoundError` do we issue the PUT.

This makes the activity truly idempotent against `DnsRecordInUse`
self-conflicts.

## Validation evidence

- `python -c "import ast; ast.parse(open('services/network.py').read())"` → OK.
- `pytest -q api/tests/` → 13 passed.
- API redeployed via `WEBSITE_RUN_FROM_PACKAGE` user-delegation SAS
  (`funcapp-netfix.zip`), Function App restarted.
- Pending: user retries Provision Terminal in the browser to confirm
  the orchestrator now passes the Network step.
