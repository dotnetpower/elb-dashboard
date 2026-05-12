# Key Vault name uniqueness across resource groups

**Date**: 2026-05-12
**Scope**: `api/activities/terminal.py`

## Motivation

Same root cause as the DNS-label and Public-IP collisions: Key Vault
names are unique across the **entire Azure cloud**, not per resource
group. The previous default `kv-elb-{vm_name[-8:]}` produced
`kv-elb-terminal` for the default VM name, which is already owned by
`rg-elb-terminal`. Provisioning a Remote Terminal in a second
resource group (e.g. `rg-elb-demo-terminal`) failed at the Key Vault
step with:

```
HttpResponseError: (VaultAlreadyExists) The vault name 'kv-elb-terminal'
is already in use.
```

The orchestrator could not progress past Key Vault even though the
caller has full access to the existing vault — the PUT was being
blocked because the name belongs to a different RG.

## User-facing change

Multiple resource groups can now provision their own Remote Terminal
without manually overriding `vault_name`. Each (subscription, RG, VM)
tuple gets its own vault:

```
kv-vm-elb-terminal-66a4dd
```

Re-running the wizard against the same RG still hits the same vault
(stable hash), so the orchestrator's idempotent path is preserved.

## API / IaC diff summary

`api/activities/terminal.py`:

- New `_default_vault_name(subscription_id, resource_group, vm_name)`
  helper. Sanitises the VM name to lowercase alphanumeric+`-`,
  appends a 6-character SHA-256 hex suffix derived from the
  `(sub, rg, vm)` tuple, and clamps the result to the 24-character
  Key Vault name limit.
- `activity_ensure_keyvault` calls the helper instead of the
  hard-coded `f"kv-elb-{vm_name[-8:]}"` when the caller did not
  supply an explicit `vault_name`.
- Pure function, no SDK calls — safe to change without orchestrator
  replay concerns.

## Validation evidence

- Local sanity check:
  - `_default_vault_name(sub, "rg-elb-terminal", "vm-elb-terminal")`
    → `kv-vm-elb-terminal-66a4dd` (24 chars).
  - `_default_vault_name(sub, "rg-elb-demo-terminal", "vm-elb-terminal")`
    → `kv-vm-elb-terminal-9f3812` (24 chars).
- `pytest -q api/tests/` → 13 passed.
- API redeployed via `WEBSITE_RUN_FROM_PACKAGE` user-delegation SAS
  alongside the network/dns idempotency fixes.
- Pending: user retries Provision Terminal in `rg-elb-demo-terminal`
  to confirm the orchestrator passes the Key Vault step.
