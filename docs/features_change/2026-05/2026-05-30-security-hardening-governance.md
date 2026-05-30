# Security hardening governance + safety net

**Audience:** maintainers, future hardening PR authors.
**Status:** governance-only change — no user-visible behaviour change. Sets up
the safety net that all subsequent audit PRs must satisfy.

## Motivation

A 30-item permission-risk audit (P0/P1/P2/P3) was extracted from the codebase
on 2026-05-30. Acting on it as a single mega-PR would have been unreviewable
and would have risked silently stripping permissions that subscription Owners
/ Contributors / Readers were relying on. The remediation is split into ~11
focused PRs, but those PRs need a shared safety net first:

1. A **charter rule** that codifies how to land RBAC narrowing, new guards,
   and SSE auth changes without breaking existing personas.
2. A **persona regression matrix** that fails CI if any of the four standard
   caller shapes loses an action it should still have.
3. A **capability probe** that hard-fails `postprovision.sh` when the deployed
   shared MI cannot exercise a required Azure surface (the first time roles
   get narrowed too aggressively).

This change ships those three artefacts and is a hard prerequisite for every
audit-remediation PR that follows.

## User-facing change

None. Pure governance + test infrastructure.

## API / IaC diff summary

| File | Kind | Summary |
|------|------|---------|
| `.github/copilot-instructions.md` | docs | New `§12a Security Hardening Discipline` section (~115 lines, 6 rules): 2-phase RBAC narrowing (ADD → soak → REMOVE), persona matrix gate, capability probe gate, default-OFF guards behind `STRICT_*` / `ENFORCE_*` env vars, SSE ticket-only auth rule, mandatory PR template block. |
| `api/tests/test_persona_matrix.py` | test | 32 parametrized tests covering four caller personas — `owner_caller` (subscription Owner), `contributor_caller` (RG Contributor + Blob Data Contributor), `reader_caller` (subscription Reader + Blob Data Reader), `dev_bypass_caller` (`AUTH_DEV_BYPASS=true`). Verifies the Reader keeps the read-only allowlist and the bypass guard fails closed when `CONTAINER_APP_NAME` is set. |
| `api/tests/persona_reader_allowlist.py` | test | 11 explicit Reader-allowed actions (dashboard browse, job list/status, logs, terminal open, AKS observe). Splitting changes to this file from enforcement changes is a §12a Rule 2 requirement. |
| `scripts/dev/probe_capabilities.py` | script | New post-deploy probe — runs one real call against each critical Azure surface (`BlobServiceClient.list_containers`, `TableServiceClient.list_tables`, `ContainerRegistryManagementClient.registries.get`, `ManagedClustersOperations.get` when AKS exists, `KeyClient.list_properties_of_keys`, `ContainerAppsAPIClient.container_apps.get`) using the shared user-assigned MI. A 403 / `AuthorizationFailed` aborts with a non-zero exit and points at the granting Bicep module. |
| `scripts/dev/postprovision.sh` | script | Wires `probe_capabilities.py` as the final step of every deploy. `quick-deploy.sh all` must not skip it. |

## Validation evidence

```
$ uv run pytest -q api/tests/test_persona_matrix.py
32 passed in <2s

$ uv run pytest -q api/tests
2007 passed, 3 skipped in 53.08s
```

Capability probe is exercised end-to-end as part of the next `azd up` /
`postprovision.sh` run — by design, no synthetic stub. The probe itself only
calls SDK list / get operations so the cost is negligible.

## Follow-up PRs

This change is the prerequisite for the audit-remediation PR series
(`P0 #1` → `P3 #30`). Each subsequent PR MUST include the §12a Rule 6 PR
checklist block in its description and keep `test_persona_matrix.py` green.

§12a Rule 1 reminder: any RBAC narrowing PR must land as **phase-1 (ADD)**
first, then a separate **phase-2 (REMOVE)** PR after a 7-day soak.
