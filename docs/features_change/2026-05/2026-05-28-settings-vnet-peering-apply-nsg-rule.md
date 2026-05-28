---
title: Settings — VNet peering "Apply NSG rule" action
description: Opt-in inbound-allow rule on the target subnet's NSG (TCP 80, 443) when the post-peering probe fails.
tags:
  - user-guide
  - operate
  - security
---

# Settings — VNet peering "Apply NSG rule" action

## Motivation

After the operator pairs the AKS auto-VNet with a target workload VNet
from the dashboard, the dashboard issues a probe HTTP request from the
api sidecar to `target_ip` (default `10.224.0.7`) to confirm the path
works. The peering itself is a control-plane operation and always
succeeds when ARM accepts it, but the probe fails (`reachable: false`)
when the target subnet's NSG drops the inbound TCP packet — the most
common cause of the "peering OK but app unreachable" report.

Previously the dashboard could only print the failure and let the
operator open Azure Portal to authore the inbound rule by hand. This
session adds an explicit, opt-in "Apply NSG rule" button that writes
the minimum-blast-radius rule from the dashboard's identity when the
caller has `Microsoft.Network/networkSecurityGroups/securityRules/write`
on the target NSG.

## User-facing change

* New panel under **Settings → VNet peering** that appears only when
  the post-peering probe reports `reachable: false`.
* Button **"Apply NSG rule (80, 443)"** — single click writes one
  inbound-allow security rule.
* When the caller's identity lacks write permission, the panel renders
  a one-line `az network nsg rule create` snippet (with **Copy CLI**)
  scoped to the same shape the dashboard would have applied, so the
  operator can hand it to a privileged admin.
* After a successful apply the dashboard automatically re-runs the
  probe and updates the existing peering result line.

## Safety contracts (NON-NEGOTIABLE)

1. **Source CIDRs are pulled from the AKS VNet's `address_space.address_prefixes`,
   never from caller input.** The route ignores any source field a
   caller might add to the body.
2. **Destination is pinned to `target_ip/32`** — IPv4 only; IPv6,
   link-local, multicast, loopback, and unspecified addresses are
   rejected with HTTP 400 (same `_validate_target_ip` gate the probe
   uses).
3. **Ports are clamped to the allowlist `{80, 443}`.** Any value
   outside the allowlist returns HTTP 400.
4. **Rule name is deterministic:** `elb-dashboard-allow-aks-<sha256(aks_vnet_id|destination_ip)[:8]>`.
   This is the only name the route ever writes.
5. **Idempotent re-run:** if a rule with that exact name already exists
   and its (source, destination, ports, access=Allow, direction=Inbound)
   shape covers the requested shape, the route returns
   `applied=true, skipped_reason="already_present"` and makes **zero**
   ARM write calls.
6. **Name collision refuses to overwrite operator rules:** same name,
   different shape returns `applied=false, skipped_reason="name_collision"`
   with the existing rule summary attached.
7. **Reserved priority window `4000–4096`.** The route fills the lowest
   free slot; if all 97 slots are taken (vanishingly rare) it returns
   `applied=false, skipped_reason="no_free_priority"`.
8. **Permission check is fail-closed.** Any exception while listing
   permissions defaults to `False` so a transient ARM hiccup cannot
   become a permissive bypass.

## API / IaC diff summary

### New files

* [`api/tasks/azure/peering_nsg.py`](../../../api/tasks/azure/peering_nsg.py)
  — the only module that imports `AuthorizationManagementClient` and
  calls `network_client.security_rules.begin_create_or_update`. Holds:
  * `resolve_vnet_pair_for_cluster()` — re-uses `peering._resolve_aks_node_vnet`
    + `_resolve_vnet_id` so both routes resolve the AKS auto-VNet the
    same way.
  * `resolve_nsg_context()` — locates the target subnet by IPv4 CIDR
    containment, surfaces its NSG id (or `None` when unattached).
  * `has_nsg_write_permission()` — `permissions.list_for_resource` against
    the NSG scope, fail-closed.
  * `apply_inbound_allow_rule()` — guards + idempotency + priority
    picker + the single `begin_create_or_update` call.
* [`api/tests/test_peering_nsg.py`](../../../api/tests/test_peering_nsg.py)
  — 16 unit tests (subnet resolution, RBAC wildcard / NotActions /
  exception fail-closed, IPv4 + port + empty-source guards, idempotency,
  name collision, free-priority selection, ARM body shape).

### Changed files

* [`api/routes/settings/vnet_peering.py`](../../../api/routes/settings/vnet_peering.py)
  — new `POST /api/settings/vnet-peering/apply-nsg-rule`. Reuses
  `_validate_target_ip` and `_nsg_cli_hint` for the
  `permission_denied` branch. Every 4xx and 5xx response is logged with
  `caller_oid` for audit.
* [`web/src/api/settings.ts`](../../../web/src/api/settings.ts)
  — typed client `settingsApi.applyPeeringNsgRule()` + the matching
  request / response types (`VnetPeeringNsgRuleRequest`,
  `VnetPeeringNsgContext`, `VnetPeeringNsgRuleApplied`,
  `VnetPeeringNsgSkipReason`, `VnetPeeringNsgRuleResponse`).
* [`web/src/components/SettingsPanel.tsx`](../../../web/src/components/SettingsPanel.tsx)
  — new `NsgRuleAction` widget rendered inside the existing
  `VnetPeeringSection` only when the last probe reports `reachable: false`.
* [`api/tests/test_settings_vnet_peering.py`](../../../api/tests/test_settings_vnet_peering.py)
  — 7 new route tests (IPv4 guard, port allowlist, no-NSG branch,
  target-ip-not-in-subnet branch, permission-denied returns the CLI
  hint, applied branch verifies SSRF-safe source CIDR + ports, 404 on
  missing AKS cluster).
* [`api/tests/test_tasks_facade_contract.py`](../../../api/tests/test_tasks_facade_contract.py)
  — `_FACADE_CONTRACT` extended with the four new
  `api.tasks.azure.peering_nsg.*` monkeypatch targets.

No IaC change. No new dependency.

## Validation evidence

* `uv run ruff check api/tasks/azure/peering_nsg.py api/routes/settings/vnet_peering.py api/tests/test_peering_nsg.py api/tests/test_settings_vnet_peering.py` — **All checks passed.**
* `uv run pytest api/tests/test_peering_nsg.py api/tests/test_settings_vnet_peering.py -q` — **27 passed in 4.49 s**.
* `uv run pytest api/tests/test_tasks_facade_contract.py api/tests/test_peering_nsg.py api/tests/test_settings_vnet_peering.py api/tests/test_azure_peering.py api/tests/test_smoke.py -q` — **148 passed in 10.13 s**.
* `uv run pytest api/tests -q` — **1711 passed, 5 failed, 3 skipped**.
  The 5 failures are pre-existing on `api/routes/storage/prepare_db.py`
  (`AttributeError: 'dict' object has no attribute 'healthy'` — a
  contract mismatch between `_try_dispatch_aks_mode` and
  `get_cluster_health`). That code path is **not** touched by this
  change set and was already dirty in the working tree before this
  session. Tracking separately.
* `cd web && npm run build` — **built in 10.44 s**, no errors.
* `cd web && npx vitest run` — **394 passed (53 files)**.
