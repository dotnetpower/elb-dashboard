---
title: Per-deployment control-plane toggles survive every redeploy
description: quick-deploy now imports azd env unconditionally and load_azd_env falls back to the on-disk .azure/<env>/.env file, so an azd-env pin like SERVICEBUS_ENABLED=true is no longer reset to the repo default on a redeploy that passes the core target vars explicitly.
tags:
  - infra
  - operate
---

# Per-deployment control-plane toggles survive every redeploy

## Motivation

The optional Service Bus integration (and its dashboard **Message Flow** card)
kept disappearing after a redeploy. The card renders only when
`service_bus_enabled()` is true, which requires the env gate
`SERVICEBUS_ENABLED` to be truthy on the `api` / `worker` / `beat` sidecars.

Root cause was in the deploy path, not the app:

1. `scripts/dev/quick-deploy.sh` only called `load_azd_env` **when one of the
   four core target vars** (`AZURE_RESOURCE_GROUP`, `ACR_NAME`,
   `ACR_LOGIN_SERVER`, `CONTAINER_APP_NAME`) was unset. The normal moonchoi
   deploy passes all four explicitly, so azd env was skipped entirely and the
   per-deployment pin `SERVICEBUS_ENABLED=true` (stored only in azd env) never
   reached `control_plane_env_pairs` — it fell back to the
   `infra/control-plane-env.json` default `"false"` and every such redeploy
   reset the live env to `false`.
2. Even when `load_azd_env` did run, `azd env get-values` blocks on an
   interactive `Select an environment to use:` prompt (no
   `.azure/config.json` default environment), gets killed by the 8 s timeout,
   and leaves the **prompt text** on stdout — which is non-whitespace, so the
   old "is the output empty?" check was fooled and no fallback fired.

## User-facing change

No UI change. Operationally: a control-plane toggle pinned in azd env
(`azd env set SERVICEBUS_ENABLED true`) now stays applied across every
`quick-deploy.sh` redeploy instead of silently reverting to the repo default.
The repo default stays OFF (charter §12a Rule 4); only an explicit azd-env (or
CLI/file) value turns it on.

## API / IaC diff summary

- `scripts/dev/quick-deploy.sh` — `load_azd_env` is now called
  **unconditionally** in the deploy preamble (it only fills UNSET keys via the
  `${!key+x}` guard, so explicit CLI/file target overrides are untouched).
- `scripts/dev/lib-env.sh` —
  - `load_azd_env` redirects `azd env get-values` stdin from `/dev/null` so the
    CLI can never hang on an interactive prompt (fails fast instead of burning
    the timeout).
  - The "CLI produced usable data?" test now checks for an actual
    `KEY=VALUE` assignment line, not merely non-empty output, so a killed
    prompt no longer suppresses the fallback.
  - New best-effort `_azd_env_file` resolver + a direct-read fallback to
    `.azure/<env>/.env` (resolution order: `$AZURE_ENV_NAME` →
    `.azure/config.json` `defaultEnvironment` → the sole `.azure/*/` dir). This
    keeps the pin flowing even when the `azd` CLI is absent / not logged in /
    slow.
- No Bicep / infra template change. The `azd provision` path already wired
  `serviceBusEnabled` from `${SERVICEBUS_ENABLED=}`
  (`infra/main.parameters.json`); this fix closes the parallel gap in the
  `quick-deploy.sh` `--set-env-vars` path.

## Validation evidence

- `bash -n scripts/dev/lib-env.sh scripts/dev/quick-deploy.sh
  scripts/dev/tests/test_lib_env.sh` — clean.
- `bash scripts/dev/tests/test_lib_env.sh` — ALL PASS, including the new case
  *"load_azd_env falls back to .azure/&lt;env&gt;/.env when CLI yields nothing"*
  (asserts the pin is imported AND an explicit export is preserved).
- Real-repo smoke (clean `env -i`): `load_azd_env` imports
  `SERVICEBUS_ENABLED=true` and the correct `AZURE_SUBSCRIPTION_ID` in **0 s**
  (file fallback, no prompt wait).
- End-to-end: with the four core target vars pre-set (the flow that previously
  skipped azd env), `control_plane_env_pairs` now emits
  `SERVICEBUS_ENABLED=true` for `api`, `worker`, and `beat`.
- `uv run pytest -q api/tests/test_control_plane_env.py` — 10 passed.
- Live remediation (immediate): re-applied `SERVICEBUS_ENABLED=true` to the
  three sidecars (revision `0000390`, RunningAtMaxScale); `/api/settings/
  service-bus` → `effective_enabled: true`; `/api/monitor/message-flow` →
  `enabled: true`, `active_total: 30`.
