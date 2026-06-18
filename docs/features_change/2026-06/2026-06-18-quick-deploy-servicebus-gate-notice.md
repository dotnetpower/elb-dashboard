---
title: quick-deploy surfaces the Service Bus master-switch state (Message Flow visibility)
description: quick-deploy.sh now prints a one-time deploy notice when SERVICEBUS_ENABLED is not pinned ON, explaining why the Message Flow card stays hidden and the exact command to enable it — without flipping the charter §12a Rule 4 default-OFF gate.
tags:
  - operate
  - blast
---

# quick-deploy Service Bus gate notice

## Motivation

The dashboard **Message Flow** card renders only when the optional Service Bus
integration is effective-enabled, which requires **two** independent gates
(`api/services/service_bus_pref.py` `service_bus_enabled()`):

1. the deploy-time master switch `SERVICEBUS_ENABLED` on the api / worker / beat
   sidecars, and
2. a saved Service Bus namespace in **Settings → Service Bus integration**
   (the Table-backed config row).

The repo default for gate 1 is OFF (`infra/control-plane-env.json` →
`SERVICEBUS_ENABLED: ""`), intentionally, per charter §12a Rule 4 (new guards
ship default-OFF) and the 2026-06-18 env-gate remediation decision. The recurring
operator confusion — including "why is Message Flow missing on another tenant's
deployment?" — is that after `quick-deploy.sh` the card silently stays hidden
with **no hint** about which gate is off or how to turn it on. The override
mechanism already exists (`azd env set SERVICEBUS_ENABLED true` survives every
redeploy via `load_azd_env` + `control_plane_env_pairs`), but it is undiscoverable
at deploy time.

## User-facing change

`scripts/dev/quick-deploy.sh` now emits a **one-time, informational** notice
during any deploy that patches a control-plane sidecar (`api`/`worker`/`beat`)
when the resolved `SERVICEBUS_ENABLED` is **not** pinned truthy:

```
i Service Bus master switch SERVICEBUS_ENABLED is not pinned ON — the Message Flow card stays hidden.
  Enable it for this deployment (survives redeploys): azd env set SERVICEBUS_ENABLED true  (then rerun this deploy)
  Message Flow also needs a Service Bus namespace saved in Settings -> Service Bus integration.
```

- Printed at most once per deploy run (guarded by `_SB_GATE_NOTICE_DONE`).
- Silent when `SERVICEBUS_ENABLED` resolves to a truthy value
  (`true`/`1`/`yes`/`on`) — i.e. the card can render, so no nudge is needed.
- Resolution mirrors `control_plane_env_pairs` precedence exactly: a SET
  process/azd env value wins over the JSON default; unset falls back to the
  JSON (`""` = defer to the Settings config row).

This does **not** flip any gate, change any default, or alter the two-gate
model. It is purely a discoverability nudge for the existing opt-in path.

## API / IaC diff summary

- Deploy script only. No backend, route, Bicep, RBAC, or env-default change.
  - [scripts/dev/quick-deploy.sh](../../../scripts/dev/quick-deploy.sh): added the
    `servicebus_gate_notice` helper (one-time, charter §12a-aligned) and a
    `case "$tgt" in api|worker|beat) servicebus_gate_notice ;;` call in both the
    `all`-branch and single-sidecar PATCH loops.
- `infra/control-plane-env.json` `SERVICEBUS_ENABLED` stays `""` (default-OFF,
  charter §12a Rule 4). No security gate is enabled by this change.

## Validation evidence

- `bash -n scripts/dev/quick-deploy.sh` — syntax OK.
- Isolated helper test across the four resolution states:
  - unset (JSON default `""`) → notice printed once; a second call is silent.
  - `SERVICEBUS_ENABLED=true` → silent.
  - `SERVICEBUS_ENABLED=false` → notice.
  - `SERVICEBUS_ENABLED=` (explicit empty) → notice.
