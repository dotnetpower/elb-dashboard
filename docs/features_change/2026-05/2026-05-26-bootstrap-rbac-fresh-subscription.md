# 2026-05-26 — Bootstrap-mode RBAC for fresh-subscription cluster creation

## Motivation

After commit `bc0fcf1` (2026-05-23, "fix(aks): ensure resource group exists
before AKS provisioning") the AKS provision Celery task calls
`rc.resource_groups.create_or_update(<cluster_rg>, ...)` as its first ARM
action under the **dashboard MI**. The MI has only `Reader` at subscription
scope (intentional, per `infra/modules/subscriptionRoles.bicep`), so on a
fresh subscription where `rg-elb-cluster` does not exist yet the user hits:

```
(AuthorizationFailed) The client '…' with object id '<oid>' does not have
authorization to perform action 'Microsoft.Resources/subscriptions/
resourcegroups/write' over scope '/subscriptions/<sub>/resourcegroups/
rg-elb-cluster' or the scope is invalid.
```

`cli-upgrade.sh full` did not surface or fix this gap because its sole RBAC
preflight `grant-runtime-rbac.sh`:

- silently returned `0` with `"no AKS cluster found in subscription; skipping
  (nothing to grant)"` when no AKS existed yet (= every first-time deploy);
- `die`d when `--cluster-rg` referenced a not-yet-existing RG; and
- only ever granted at *existing* AKS RG scope, never the future cluster RG.

So a user that did `git fetch && git checkout main && cli-upgrade.sh full
--yes` and then clicked **Create Cluster** in the SPA got an actionable-
looking but dead-end error card (the CTA was a generic Microsoft Learn
link). This change closes the gap end-to-end without escalating the MI to
subscription-scope Contributor.

## User-facing change

- **`grant-runtime-rbac.sh`** gains a **bootstrap mode**:
  - `--region <region>` (alias `--location`) — when `--cluster-rg` refers
    to a non-existent RG, the script creates it in `<region>` and then
    grants `Contributor` + `User Access Administrator` on that RG.
  - When AKS list is empty and no `--cluster-rg` was passed, the script
    no longer silent-skips. It prints the exact `bash scripts/dev/
    grant-runtime-rbac.sh --cluster-rg … --region … --yes` invocation
    that will pre-create the RG and grant the roles, then exits `0`.
  - When `--cluster-rg` refers to a missing RG and no `--region` was
    passed, the script dies with exit `3` and prints the corrective
    one-liner — instead of the old generic `"AKS cluster RG '<x>' not
    found"` die.
  - Help text and usage block expanded; `usage()` slice range updated.
- **`cli-upgrade.sh`** detects the "no AKS cluster" silent-skip branch
  from `grant-runtime-rbac.sh` output and re-prints a clear *First-time-
  cluster-create RBAC note* at the very end of a successful upgrade,
  with the same actionable command. The note is suppressed when the
  preflight grant was satisfied.
- **`armErrorClassifier.ts`** `rg_permission` branch:
  - parses `with object id '<oid>'` and `over scope '/subscriptions/
    <sub>/resource[gG]roups/<rg>'` out of the raw ARM error;
  - emits a new `command`-kind action containing the **fully-baked**
    `az role assignment create … --role Contributor --scope …/<rg>`
    snippet (plus an optional `az group create` step with a `<region>`
    placeholder);
  - keeps the docs link, but renames it to "Grant Contributor role
    (docs)" so the primary CTA is the copy-command button.
- **`ProvisionErrorCard.tsx`** renders `command`-kind actions as a
  clipboard-copy button (`navigator.clipboard.writeText` with an
  `execCommand("copy")` fallback) and shows a transient `"Copied!"`
  label so the operator gets immediate feedback. Tooltips expose the
  full command on hover.

## API / IaC diff summary

| File | Change |
|------|--------|
| `scripts/dev/grant-runtime-rbac.sh` | Add `--region` / `--location` flag, bootstrap-mode `az group create` branch, actionable AKS-0 hint, updated usage block, `usage()` slice range. |
| `scripts/dev/cli-upgrade.sh` | Init `NEEDS_BOOTSTRAP_NOTE=0`; capture preflight output and detect the AKS-0 branch; new `emit_bootstrap_note_if_needed` called at success exit. |
| `web/src/components/cards/ClusterCard/armErrorClassifier.ts` | Extend `ArmErrorAction.kind` with `"command"`; new `parseAuthFailure` + `buildGrantContributorCommand` helpers; rg_permission branch rewritten to include the concrete command action. |
| `web/src/components/cards/ClusterCard/ProvisionErrorCard.tsx` | Import `Check`/`Copy`/`useState`; new `CommandActionButton` (clipboard copy with fallback); `actions.map` switches on `kind === "command"`. |
| `web/src/components/cards/ClusterCard/armErrorClassifier.test.ts` | New test case pinning the concrete-command output for the canonical fresh-subscription error message; the existing 5 cases still pass unchanged. |
| No Bicep changes. | Subscription-scope RBAC stays at Reader-only by design (least privilege). |

## Validation evidence

- `bash -n scripts/dev/grant-runtime-rbac.sh && bash scripts/dev/
  grant-runtime-rbac.sh --help` — usage block renders the new bootstrap
  examples.
- `bash -n scripts/dev/cli-upgrade.sh` — syntax OK.
- `cd web && npx vitest run src/components/cards/ClusterCard/
  armErrorClassifier.test.ts` → **6 / 6 passed** (5 existing + 1 new).
- `cd web && npx eslint src/components/cards/ClusterCard/{armErrorClassifier.ts,
  ProvisionErrorCard.tsx,armErrorClassifier.test.ts}` → clean.
- `uv run pytest -q api/tests/test_azure_provision_aks.py` → **7 / 7 passed**
  (regression guard on the `rg.create_or_update → aks.begin_create_or_update`
  ordering introduced by `bc0fcf1`; unchanged by this PR).

## Out of scope

- No change to Bicep RBAC. Subscription-scope Contributor for the MI was
  considered and rejected — it would let any compromised api / worker
  sidecar create arbitrary resources across the whole subscription.
- No automation of `--region` selection inside `cli-upgrade.sh`. The
  operator picks the region; the script refuses to guess.
- The SPA still does not run shell commands directly — the rg_permission
  CTA copies the command to clipboard and the user runs it from their
  own terminal with their own `az login` (which is, in the recurring
  case, an Owner / UAA holder who can grant the role).
