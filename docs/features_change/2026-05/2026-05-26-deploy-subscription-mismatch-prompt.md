# deploy.sh: interactive subscription/tenant mismatch prompt

## Motivation

`./deploy.sh` previously errored out hard whenever the active Azure CLI
subscription/tenant differed from the values stored in
`.azure/<env>/.env` (typical after `az login` against a different
subscription on the same workstation). The error message printed two
remediation paths but required the operator to manually copy/paste
commands and rerun the script — a friction point that confused the user
in the May 26 session.

## User-facing change

When the script detects a subscription or tenant mismatch in an
interactive TTY, it now prints both contexts and asks:

```
How would you like to proceed?
  1) Use the active Azure CLI context — retarget the azd environment to it (sets ELB_ALLOW_AZD_ENV_RETARGET=true)
  2) Keep the azd environment target — switch Azure CLI to match it (runs az account set [+ az login --tenant])
  3) Abort and let me fix it manually
Enter choice [1/2/3] (default 3):
```

* **Option 1** (the natural choice when the operator just ran `az login`
  against the intended subscription) sets `ELB_ALLOW_AZD_ENV_RETARGET=true`
  in the current process; the downstream `azd env set ... AZURE_SUBSCRIPTION_ID`
  calls then overwrite `.azure/<env>/.env` to match the active Azure CLI
  context (the documented retarget behaviour).
* **Option 2** runs `az login --tenant <existing-tenant>` (only when the
  tenant differs) followed by `az account set --subscription <existing-sub>`,
  then re-reads `az account show` and continues. The existing
  `azd auth login --check-status` block immediately afterwards handles
  re-logging azd when needed.
* **Option 3 (default)** prints the same manual remediation block the
  script used to emit and exits 1.

Non-interactive shells (no TTY on stdin/stdout, e.g. CI) skip the prompt
and behave exactly like before: print the remediation block and exit 1.
`ELB_ALLOW_AZD_ENV_RETARGET=true` still skips the entire check, same as
before.

## API / IaC diff summary

* [deploy.sh](../../../deploy.sh): replaced the two separate
  subscription/tenant mismatch error blocks with a single combined
  detection + interactive prompt. Help text for
  `ELB_ALLOW_AZD_ENV_RETARGET` updated to describe the new prompt and
  the non-interactive fallback.

No backend, frontend, or Bicep changes.

## Validation

* `bash -n deploy.sh` — syntax clean.
* Manual flow inspection: prompt branches handle both `subscription`-only
  and `subscription + tenant` mismatches; option 1's `az account set`
  verifies the switch took effect before continuing.
