---
title: Self-upgrade az login — bootstrap MSI account context for az acr build
description: Fix the self-upgrade build failing with "Please run 'az login' to setup account" by bootstrapping a managed-identity az login into the terminal sidecar exec cache and ensuring it before each build.
tags:
  - operate
  - deployment-reference
---

# Self-upgrade az login — bootstrap MSI account context for az acr build

## Motivation

After fixing the earlier self-upgrade blockers (PLATFORM_ACR_NAME, failed_pre
restart, empty-clone detection), the build reached `az acr build` but failed
with:

```
$ az acr build --registry … --image elb-api:… --file api/Dockerfile … /tmp/elb-upgrade/<job>
ERROR: Please run 'az login' to setup account.
```

## Root cause

The self-upgrade `az acr build` runs in the **terminal sidecar** via the exec
server, which uses a dedicated `AZURE_CONFIG_DIR` (`/tmp/elb-exec-azure`)
separate from the interactive browser shell's `~/.azure`. The entrypoint
created that directory but never ran an `az login` into it. `azcopy` and
`kubectl` authenticate with the managed identity directly (no `az` account
needed), so the gap only surfaced for the `az acr build` code path, which
requires an `az` CLI account context.

## User-facing change

Self-upgrade builds now find a managed-identity `az` account context and
proceed past `az acr build` setup. No dashboard surface changes.

## API / IaC diff summary

- `terminal/entrypoint.sh`: after starting the exec server, background a
  best-effort `az login --identity` into the exec `AZURE_CONFIG_DIR`
  (`--username $AZURE_CLIENT_ID` for the shared user-assigned MI, falling back
  to a system-assigned login). Backgrounded + best-effort so a login hiccup
  never blocks ttyd / exec_server startup.
- `api/services/upgrade/image_builder.py`: new `ensure_exec_az_login(runner)`
  that runs `az login --identity [--username <client_id>] --allow-no-subscriptions`
  in the terminal sidecar. Idempotent (login is a no-op refresh when already
  signed in); failures are logged and swallowed so the build's own account
  re-check surfaces the real error.
- `api/tasks/upgrade/pipeline.py`: call `ensure_exec_az_login` once after the
  clone, before the build loop, to close the async-bootstrap race
  deterministically.
- No infra/Bicep change (the terminal sidecar already carries `AZURE_CLIENT_ID`).

## Validation evidence

- Live build log before the fix (terminal already refreshed, clone now
  succeeds): `az acr build … /tmp/elb-upgrade/<job>` → `ERROR: Please run
  'az login' to setup account.` — confirming the missing account context was
  the only remaining blocker.
- New tests: `test_ensure_exec_az_login_uses_uami_client_id`,
  `test_ensure_exec_az_login_system_identity_when_no_client_id`,
  `test_ensure_exec_az_login_swallows_failures`.
- `bash -n terminal/entrypoint.sh` → syntax OK.
- `uv run pytest -q api/tests` → 3042 passed, 3 skipped.
- `uv run ruff check` on touched files → clean.
