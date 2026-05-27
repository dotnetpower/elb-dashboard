# Manual deploy scripts: az login is the source of truth (auto-sync azd env)

## Motivation

`scripts/dev/quick-deploy.sh` (two call sites) and `scripts/dev/cli-upgrade.sh`
both started with the same block:

```bash
if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi
```

That silently switched the active `az login` subscription to whatever
`azd env get-values` had set. The trap: a developer looking at
`az account show` would see (and trust) one subscription, but the deploy
script would build the image into and PATCH the Container App in a
**different** subscription's ACR / Container App — because the resource
names in `azd env` (`ACR_NAME`, `CONTAINER_APP_NAME`, …) all live in that
other subscription. This pattern has caused "pushed image to the wrong
tenant" incidents.

The operator's intent on a fresh shell is always "use the subscription I am
currently logged into" — `az account show`. So the right behavior is the
opposite direction: rewrite **azd env** to match `az account show`, never
flip `az login` behind the operator's back.

## User-facing change

`quick-deploy.sh` and `cli-upgrade.sh` now treat `az account show` as the
single source of truth. They do **not** auto-switch the active `az login`
subscription. When `AZURE_SUBSCRIPTION_ID` (from azd env) differs from
the active `az account show` subscription, the script:

1. Prints the mismatch (both subscription IDs + the azd env name).
2. Runs `azd env set AZURE_SUBSCRIPTION_ID <current>` (and `AZURE_TENANT_ID`
   when it also differs), persisting to `.azure/<env>/.env` so the next
   shell starts aligned too. Best-effort — failures log a warning and the
   deploy proceeds with an in-process export instead.
3. Exports both values in-process so the rest of the current deploy uses
   the az-login subscription.
4. Continues with the build/PATCH.

When `AZURE_SUBSCRIPTION_ID` is unset, the script trusts `az account show`
and exports it for the rest of the deploy. When they match, the script
proceeds silently after a one-line `[az-context] active subscription: …
(aligned with azd env)` info log.

No CLI flag change. The behavior applies uniformly to
`quick-deploy.sh <api|frontend|terminal|all> [tag]` and
`cli-upgrade.sh <api|frontend|terminal|full|rollback>`.

`scripts/dev/postprovision.sh` is intentionally **not** changed — it runs
as an azd hook (`azd up` / `azd provision`) where azd env IS authoritative
and aligning `az` silently is the right behavior. It carries a comment
explaining why.

## API / IaC diff summary

- New: `scripts/dev/az-context.sh` — sourced helper exporting
  `assert_az_subscription_aligned()`. The helper:
  - Aborts only on "not logged in" (`az account show` fails).
  - Trusts az login when `AZURE_SUBSCRIPTION_ID` is unset (exports it).
  - On match, logs one info line and returns 0.
  - On mismatch, calls `azd env set` (best-effort), updates the
    in-process environment, logs the sync, returns 0 — the deploy
    continues against the az-login subscription.
- `scripts/dev/quick-deploy.sh` — sources `az-context.sh`; both
  silent `az account set` blocks replaced with `assert_az_subscription_aligned`.
- `scripts/dev/cli-upgrade.sh` — sources `az-context.sh`; the preflight
  `if ! az account show; then die; fi` + silent `az account set` block
  replaced with `assert_az_subscription_aligned`.
- `docs/operate/cli-upgrade.md` — preflight checklist row updated to
  describe the auto-sync behavior.
- No Python / frontend / Bicep changes.

## Validation evidence

- `bash -n scripts/dev/{az-context,quick-deploy,cli-upgrade}.sh` → all syntax OK.
- `grep -n "az account set\|assert_az_subscription_aligned" scripts/dev/*.sh`
  confirms: 2 call sites in `quick-deploy.sh` + 1 in `cli-upgrade.sh` now
  call the helper; the only remaining `az account set` is in
  `postprovision.sh` (azd-hook context, intentional).
- Functional smoke under `set -Eeuo pipefail` (mirrors the deploy
  scripts' caller mode):
  - **unset**: helper exports `AZURE_SUBSCRIPTION_ID=<az-login-sub>`,
    returns 0, caller continues.
  - **match**: helper logs `aligned with azd env`, returns 0.
  - **mismatch**: helper logs the diff, attempts `azd env set` (warns if
    no env selected), exports both in-process, returns 0; caller
    continues. Verified with `(reached continuation; script does not
    abort)` print after the call.
- Earlier-iteration SIGPIPE bug fix retained: the helper does not pipe
  `azd env get-name` into `head`, so `set -Eeuo pipefail` no longer
  kills the deploy with a silent exit 141 right after the opening banner.

## Risks / non-goals

- This guard does **not** validate that `ACR_NAME` / `CONTAINER_APP_NAME`
  actually exist in the new az-login subscription. If they don't, the
  deploy fails at `az acr build` / `az containerapp update` with a clear
  `ResourceNotFound`. For workspaces where the same resource names exist
  in multiple subscriptions (the common case for this repo's
  side-by-side prod environments), the sync just works.
- No change to `azd up`/`azd provision`/`postprovision.sh` — those still
  resolve subscription via azd's own env file.
- The auto-sync writes to `.azure/<env>/.env`. If you intentionally want
  a different subscription in azd env than in `az login`, switch `az` to
  match before running the deploy (`az account set --subscription
  $AZURE_SUBSCRIPTION_ID`) — the helper will then see them aligned and
  not rewrite azd env.
