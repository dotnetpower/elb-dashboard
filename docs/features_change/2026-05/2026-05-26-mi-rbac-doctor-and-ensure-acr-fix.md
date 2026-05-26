# 2026-05-26 — MI RBAC doctor + ensure_acr MI grant + ACR role-ID typo fix

## Motivation

A review of which permissions `deploy.sh` and `cli-upgrade.sh` actually
guarantee on the deployed dashboard MI surfaced three gaps:

1. **`ensure_acr` (SPA "Create new ACR" wizard) never granted the shared
   MI any role.** It only granted the *caller user*. Worse, the constant
   it used was mislabelled: `ACR_PULL_ROLE_ID = "8311e382\u2026"` is in fact
   the **AcrPush** GUID. So the caller got AcrPush (which they don't
   need from a browser) and the MI got nothing. The next ACR Task /
   image pull from a wizard-created ACR therefore failed with
   `AuthorizationFailed`.
2. **No drift detection.** Neither script verified that the deployed
   MI's current role assignments still match what the code expects.
   `azd down` + `azd up` produces a new MI principalId; Bicep
   re-creates the in-Bicep assignments under the new principal, but
   any assignment granted *outside* Bicep (workload Storage/ACR
   attached via the SPA wizard, the AKS cluster RG, a pre-existing
   ACR) is orphaned and silently broken.
3. **"Use existing resource" path leaves MI unrelated.** The SPA can
   attach the dashboard to a pre-existing Storage / ACR that Bicep
   never touched. No code path grants the MI RBAC on that resource.

## User-facing change

### Code fix \u2014 `ensure_acr` now mirrors `ensure_storage_account`

- **`api/services/monitoring/provisioning.py`**
  - Fixes the mislabelled constant: `ACR_PULL_ROLE_ID` now carries the
    real AcrPull GUID (`7f951dda\u2026`).
  - Adds `ACR_PUSH_ROLE_ID` (`8311e382\u2026`) and `ACR_CONTRIBUTOR_ROLE_ID`
    (`b24988ac\u2026`).
  - `ensure_acr` now grants the **shared dashboard MI** the same trio
    it gets on the platform ACR from `infra/modules/acr.bicep`:
    AcrPull + AcrPush + Contributor. The caller user keeps getting
    only AcrPull (least privilege for SPA browsing).
- **`api/services/monitoring/__init__.py`** exports the two new
  constants so callers (and tests) can reference them by name.

### New \u2014 `scripts/dev/check-mi-rbac.sh` (read-only doctor)

- Enumerates the expected `{scope, role}` matrix the dashboard MI
  needs across Subscription / Platform RG / Platform Storage / Platform
  ACR / Key Vault / Cluster RG.
- Auto-detects MI principalId from the Container App's
  `identity.userAssignedIdentities`; auto-detects scope names from
  `azd env get-values`; auto-detects cluster RG from the single AKS in
  the subscription (when unambiguous).
- Calls `az role assignment list --assignee-object-id ... --role ...
  --scope ...` for each row and reports OK / WARN / FAIL.
- For every FAIL, prints the **exact** `az role assignment create`
  command an operator (or a tenant/sub admin) can paste to fix it.
  Does NOT mutate RBAC \u2014 the operator decides what to apply.
- Flags include `--cluster-rg`, `--principal-id`, `--subscription`,
  `--storage`, `--acr`, `--keyvault`, `--quiet`, `--strict`. `--strict`
  exits 1 when any FAIL exists so CI can gate on it.

### Wiring

- **`deploy.sh`** calls the doctor near the end of a successful
  `azd up`, after `grant-local-rbac.sh`. Best-effort, non-blocking.
- **`scripts/dev/cli-upgrade.sh`** calls the doctor after a successful
  `/api/health/ready=200`, after the existing bootstrap-mode note.
  `--quiet` so only WARN/FAIL lines surface.

## API / IaC diff summary

| File | Change |
|------|--------|
| `api/services/monitoring/provisioning.py` | Fix mislabelled `ACR_PULL_ROLE_ID`; add `ACR_PUSH_ROLE_ID` + `ACR_CONTRIBUTOR_ROLE_ID`; `ensure_acr` grants MI AcrPull + AcrPush + Contributor. |
| `api/services/monitoring/__init__.py` | Re-export the two new constants. |
| `api/tests/test_monitoring_acr_rbac.py` | New \u2014 3 tests pinning caller+MI grant order, no-MI fall-through, and the canonical AcrPull/AcrPush GUIDs as a regression guard. |
| `scripts/dev/check-mi-rbac.sh` | New \u2014 read-only RBAC doctor (auto-detect + comparison + fix snippets). |
| `deploy.sh` | Call doctor after successful `azd up`. |
| `scripts/dev/cli-upgrade.sh` | Call doctor after `/api/health/ready=200`, before `--logs` tail. |
| No Bicep changes. | The mismatch was Python-only; the Bicep ACR module already used the correct GUIDs. |

## Validation evidence

- `bash -n` clean on `deploy.sh`, `scripts/dev/cli-upgrade.sh`,
  `scripts/dev/check-mi-rbac.sh`.
- `bash scripts/dev/check-mi-rbac.sh --help` renders the new usage block.
- `uv run pytest -q api/tests/test_monitoring_acr_rbac.py
  api/tests/test_monitoring_storage_rbac.py
  api/tests/test_azure_provision_aks.py` \u2192 **13 / 13 passed**.

## Coverage matrix (post-change)

| Concern | deploy.sh | cli-upgrade.sh | Notes |
|---|---|---|---|
| Bicep-owned MI roles (platform RG, Storage, KV, sub Reader) | applied by `azd up` (idempotent) | not re-applied; doctor reports drift | run `azd provision` to re-apply Bicep if a new role lands in a PR |
| `workloadClusterRoles.bicep` cluster RG grant | applied when `aksClusterResourceGroup` is set on azd env | not applied; doctor reports drift | set the env var on the second `azd provision` after AKS is created |
| Self-heal cluster RG (`grant-runtime-rbac.sh`) | called by `postprovision.sh` best-effort | called as preflight | both run it |
| Cluster RG bootstrap (RG missing entirely) | actionable hint only | actionable hint only | operator runs `grant-runtime-rbac.sh --cluster-rg \u2026 --region \u2026 --yes` once |
| SPA wizard "Create new ACR" \u2192 MI roles | **fixed in this PR**: AcrPull + AcrPush + Contributor auto-granted to MI | same | regression-tested |
| SPA wizard "Create new Storage" \u2192 MI roles | already correct: Blob Data Contributor auto-granted to MI | same | unchanged |
| SPA "Use existing resource" \u2192 MI roles | not auto-granted (the resource is outside our control); doctor flags as missing | same | doctor prints the exact `az role assignment create` command |
| Orphaned assignments after MI principalId change | doctor reports as missing for the new MI; old assignments stay until cleaned by hand | same | use `az role assignment delete --assignee <old-oid>` to clean orphans |

## Out of scope

- **Auto-grant by the doctor.** Tempting, but risky: any pre-existing
  workload resource the operator points the dashboard at might be in a
  different subscription / RG where the *operator running deploy.sh*
  lacks UAA. The doctor surfaces the gap and the exact fix command;
  the operator chooses whether/where to run it.

  > **Update (later same day).** Auto-grant *is* available now as an
  > opt-in path \u2014 see the "Opt-in auto-fix" section below \u2014 but it is
  > deliberately not the default for the audit-log and security-
  > regression reasons documented there.
- **Bicep schema change to add `Microsoft.Resources/subscriptions/
  resourceGroups/write` at subscription scope.** Still rejected for
  least privilege; the bootstrap path through `grant-runtime-rbac.sh`
  remains the right answer.
- **Cleanup of orphaned role assignments after `azd down`.** The
  doctor reports drift but does not delete \u2014 deletion is irreversible
  and may surprise multi-tenant operators.

## Opt-in auto-fix (added later 2026-05-26)

After review, the doctor now supports an opt-in `--auto-fix` flag that
runs the same `az role assignment create` commands inline instead of
just printing them. The default is unchanged (read-only). The opt-in
path is plumbed through both deploy.sh and cli-upgrade.sh:

| Entry point | How to enable | What happens |
|-------------|---------------|--------------|
| `scripts/dev/check-mi-rbac.sh --auto-fix` | direct flag | per-row grant; failures on individual scopes do not block the rest |
| `scripts/dev/cli-upgrade.sh --auto-fix-rbac` | new flag | post-health doctor runs with `--auto-fix` |
| `ELB_AUTO_FIX_RBAC=true ./deploy.sh` | env override | post-azd-up doctor runs with `--auto-fix` |

The doctor prints an audit banner whenever `--auto-fix` is active,
naming the current `az login` user, so the grants are traceable in
both the console output and the Azure Activity Log. RBAC propagation
(~1\u20135 minutes) is intentionally NOT waited on \u2014 cli-upgrade has
already swapped the image and the next operator action will see the
fresh roles.

Why opt-in rather than default:

1. **Audit traceability.** Silent auto-grants make "who approved this
   role assignment?" harder to answer after the fact.
2. **Security-regression detection.** A security operator might remove
   a role on purpose; a silent doctor would re-add it on every
   cli-upgrade run, masking the change.
3. **Per-scope UAA mismatch.** The operator running cli-upgrade may
   have UAA on the platform RG but not on a workload Storage account
   in a different RG. Default-on auto-fix would spam failures; opt-in
   keeps the choice explicit.

## Caller preflight (added later 2026-05-26)

Each script now refuses to start when the operator's `az login` does
not carry the role set the run needs. New shared helper
`scripts/dev/_caller-precheck.sh` exposes:

- `elb_precheck_init [<sub>]` \u2014 resolves caller `oid`/`upn`/`sub`.
  Tolerates Service Principal and Managed Identity callers (CI / azd
  in pipelines) by falling back to `az ad sp show` when
  `az ad signed-in-user show` returns nothing. If neither works it
  warns and lets the script continue \u2014 the script's own first SDK call
  will produce the canonical Azure error.
- `elb_precheck_caller_for <mode>` \u2014 hard-fails (exit 4) with a
  remediation hint when the caller lacks the role set for the mode.

| Script | Mode invoked | Why |
|--------|--------------|-----|
| `deploy.sh` | `deploy` \u2014 Owner OR (Contributor + UAA) at sub | Bicep creates RGs (sub write) and role assignments (UAA). Catching the gap up-front saves the operator from a 10-minute azd-up that leaves an orphan RG. |
| `cli-upgrade.sh <api|frontend|terminal|full>` | `upgrade-write` \u2014 Owner OR Contributor at sub | Needs to push images via `az acr build` and PATCH the Container App. |
| `cli-upgrade.sh rollback` and `cli-upgrade.sh --dry-run` | `upgrade-read` \u2014 Reader / Contributor / Owner at sub | Read-only paths; the wider role set is enough but UAA isn't required. |
| `cli-upgrade.sh --auto-fix-rbac` | extra `upgrade-autofix` check \u2014 Owner OR UAA at sub | The post-health doctor will create role assignments under the caller's identity. |
| `check-mi-rbac.sh` (default) | `doctor-read` \u2014 Reader at sub | Needs `Microsoft.Authorization/roleAssignments/read` to enumerate the MI's grants. |
| `check-mi-rbac.sh --auto-fix` | `doctor-autofix` \u2014 Owner OR UAA at sub | Auto-fix creates role assignments under the caller's identity. |

Failure output includes (a) the caller's actual roles at sub scope
(including inherited from management groups), (b) the precise
`az role assignment create` command an Owner can run to grant the
missing role, and (c) the 1\u20135 minute RBAC propagation note. The exit
code is **4** so wrappers can distinguish a missing-permission abort
from a syntax error (2) or a runtime failure (1).
