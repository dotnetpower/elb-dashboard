# 2026-05-14 — Storage public-network-access permanently disabled

## Motivation

The earlier revisions of the Container Apps migration plan still treated
`publicNetworkAccess=Disabled` on Storage as a phase-4 hardening step and
preserved the existing `auto-keep-enabled` storage-window workaround as a
fallback for ElasticBLAST. The user's hard requirement is stricter: every
Storage account in scope must be private from day 1, and the Container App must
be the only client that can reach it.

## User-facing change

None at runtime (planning document update). Operators reading the migration
plan now see a single, non-negotiable rule rather than a "best-effort,
phased" recommendation.

## Architecture diff summary

| Area | Before | After |
|------|--------|-------|
| Storage public access (platform + workload) | Phase-4 hardening; temporary window allowed during BLAST submit | `publicNetworkAccess=Disabled`, `defaultAction=Deny`, `bypass=None` from day 1; **no operational state ever flips it back** |
| `auto-keep-enabled` toggle / `bypass: AzureServices` | Documented fallback | **Removed in this migration**; anything that depends on it is re-architected |
| Container Apps Environment | "VNet-integrated" (unspecified type) | Explicitly **workload-profile environment** with `infrastructureSubnetId` on `snet-containerapps` (sized `/23`) |
| Private DNS zones | Listed as a phase-4 task | **Linked to the platform VNet before the Container App is created**, so storage hostnames resolve to private IPs from the first deploy |
| Browser downloads | Implicit (SAS to public hostname) | Streamed through the api sidecar when SAS-resolved hostname is unreachable from the user's network; SAS path is preserved only where caller has private connectivity |
| AKS / Remote Terminal access to workload Storage | Public hostname | Private endpoint resolution because both subnets live in the same platform VNet |
| Cutover checklist | Included a generic "storage public access returns to secure state" item | Replaced with explicit verification: `Disabled / Deny / None / []`, private-IP DNS resolution from inside the api sidecar, and `403 PublicAccessNotPermitted` on external curl |

## Files changed

- `docs/container-apps-migration.md`:
  - **New top-level section "Storage Network Isolation (Hard Requirement)"**
    placed immediately before "Target Architecture", covering the rules,
    Container Apps Environment requirements that make those rules enforceable,
    what the rules forbid, and the verification commands that gate cutover.
  - Decision Summary now states the hard requirement explicitly.
  - Networking Plan rewrites the "Network rules" list to remove the
    temporary-public-access escape hatch and to specify private-only access
    for platform Storage, workload Storage, Key Vault, and ACR.
  - Phase 1 ("Containerize the API") promoted to "Containerize the API on a
    private network" — VNet, subnets, private endpoints, and DNS zone links
    are provisioned **before** the Container App is created.
  - Phase 4 reduced to verification of invariants already established in
    phase 1.
  - Cutover Checklist replaces "storage public access returns to the secure
    state" with four explicit verification rows including a public-internet
    `curl` that must be rejected.
  - Risks table: the storage-public-access risk row no longer suggests a
    fallback; it documents how each previous dependency on public access has
    been re-architected (AKS in same VNet, browser downloads streamed through
    api, NCBI imports performed by worker over private endpoint).
  - Storage Plan reinforces the rule and updates the SAS guidance.
- `README.md`: Architecture Planning bullet now states the day-1 private-only
  invariant.

## Code consequences (follow-up tickets)

These are not done in this PR (planning only) but are the obvious follow-ups
the migration must complete before public access can be turned off:

1. Delete the `auto-keep-enabled` toggle wiring in
   [api/services/storage_data.py](api/services/storage_data.py) and the
   storage-window orchestrator in
   [api/orchestrators/storage_window.py](api/orchestrators/storage_window.py).
2. Replace any browser-direct SAS download path with an api sidecar streaming
   endpoint (or keep SAS only when the caller is on the platform VNet).
3. Move the Function App backend off the public Storage path so that the
   migration period can also enforce private-only on the platform Storage
   account; if that is not feasible, the Function App must be retired before
   the platform Storage account is locked down.
4. Update the `monitor/storage` UI to drop the "Enable for 5 min" affordance
   and the public/private toggle indicator, and to show an immutable
   "Private (no public access)" state with a link to the verification
   command.

## Validation evidence

Documentation-only change. Verified the doc no longer recommends or relies on
public storage access:

```bash
grep -nE "auto-keep-enabled|publicNetworkAccess.*(Enabled|window)|temporary access-window|bypass:.*AzureServices" \
  docs/container-apps-migration.md
```

Returns no matches in active recommendation text. The phrase "temporary
access-window" survives only inside the Risks table as the description of what
is being removed.
