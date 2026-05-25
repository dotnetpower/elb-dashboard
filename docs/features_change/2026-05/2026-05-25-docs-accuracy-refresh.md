---
title: Docs accuracy refresh — terminal/redis persistence, deploy script path, stale cross-refs
description: Sequential review of every published MkDocs page; corrected stale claims about terminal-home / redis Azure Files persistence, the legacy scripts/dev/deploy.sh path, and stale cross-tree links.
tags:
  - docs
---

# Docs accuracy refresh — 2026-05-25

## Motivation

User requested a full sequential review of every page rendered by `mkdocs`,
finding and fixing inaccuracies and bringing wording current. Working
autonomously while the user is unavailable.

The mkdocs nav (per `mkdocs.yml`) was walked top-to-bottom: Overview →
User Guide → Operate (Deployment Reference, CLI Rolling Update,
Architecture×5, Research Notes×2) → Contributor (3 + Agent Reference×9).
Each page was checked against the charter
(`.github/copilot-instructions.md`), `AGENTS.md`, and the actual code in
`infra/`, `api/`, `terminal/`, and `scripts/dev/`.

## User-facing change

None — these are pure documentation corrections. No code, no API surface,
no IaC, and no UI changes.

## What was corrected (current-state docs only)

Historical change notes under `docs/features_change/` were intentionally
**not** modified — they describe original design at the time of the
change and are the historical record.

### 1. `scripts/dev/deploy.sh` → `./deploy.sh`

The canonical deploy entry point lives at the repo root as `./deploy.sh`.
There is no `scripts/dev/deploy.sh`.

- `docs/get-started.md` — JSON-LD HowTo step + TL;DR admonition.

### 2. Redis sidecar is ephemeral (not AOF-persisted on Azure Files)

`infra/modules/containerAppsEnvironment.bicep` (lines 1-9) and
`infra/modules/storageState.bicep` confirm: the earlier design that
mounted `redis-data` / `terminal-home` SMB shares was removed because
SMB mounts in Container Apps require a storage account key, which
conflicts with the platform Storage account's
`allowSharedKeyAccess: false` invariant.

- `docs/user-guide/dashboard.md` — sidecar runtime description: "AOF-persisted
  on an Azure Files share" → "ephemeral (`--save '' --appendonly no`). The
  queue is rebuilt from the `jobstate` table by the beat reconciler on
  revision restart."

### 3. Terminal `/home/azureuser` is ephemeral (not `terminal-home` share)

Same root cause as #2. User files must stage to workload Storage via
`azcopy`, and `az login --use-device-code` is re-run per session.

- `docs/user-guide/terminal.md` — replaced "`$HOME` is persisted on an Azure
  Files share" with the ephemeral statement plus the staging guidance.
- `docs/copilot/browser-terminal.md` — Persistence section rewritten;
  "Reset home" lifecycle bullet replaced with note about revision swap
  discarding `/home/azureuser`.
- `docs/copilot/auth-flow.md` — Step 6 in the auth lifecycle:
  removed the claim that `~/.azure/` profile persists on `terminal-home`;
  explained ephemeral home + device-code re-login per session.
- `docs/copilot/monitoring-ui.md` — Browser Terminal card heartbeat
  description: "mtime of `~/.azure/azureProfile.json` on the
  `terminal-home` share" → "inside the sidecar's ephemeral
  `/home/azureuser`".

### 4. Broken cross-tree link

- `docs/user-guide/results.md` — Storage locked panel link
  `../../../.github/copilot-instructions.md#9-storage-network-isolation-hard-requirement`
  → `../architecture/storage-contract.md` (in-tree, user-facing).
- `docs/architecture/authentication.md` — `docs/container-apps-migration.md`
  reference → `architecture/container-apps.md` (the page moved).
- `docs/copilot/auth-flow.md` — `docs/auth.md` reference (file no longer
  exists at that path) → `docs/architecture/authentication.md`.

### 5. Stale tree snippet

- `docs/copilot/repo-layout.md` — `docs/` tree snippet listed
  `auth.md` and `container-apps-migration.md` as if at top level; updated
  to the current `docs/` layout (`architecture/`, `copilot/`, `operate/`,
  `user-guide/`, `research/`, `features_change/`).

### 6. Duplicate bullet

- `docs/operate/index.md` — removed duplicate "Architecture —
  `docs/architecture/`" bullet under "What lives here".

## API/IaC diff

None.

## Validation

`DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` →
`Documentation built in 12.56 seconds`, no warnings or broken links.

```text
INFO    -  Cleaning site directory
INFO    -  Building documentation to directory: …/site
INFO    -  Documentation built in 12.56 seconds
```

All edits cross-checked against:

- `.github/copilot-instructions.md` §9 (Storage Network Isolation invariant)
- `AGENTS.md` route map + tripwire list
- `infra/modules/containerAppsEnvironment.bicep` and
  `infra/modules/storageState.bicep` (no Azure Files mounts)
- `deploy.sh` at repo root (no `scripts/dev/deploy.sh` shim exists)
