---
title: Automatic DB order oracle lifecycle
description: Add durable task-backed order-oracle automation after Auto warm, with bounded retries, progress, provenance, and reference-safe retention.
tags: [blast, ui, operate]
---

# Automatic DB order oracle lifecycle

## Motivation

Building a DB order oracle required a manual button after every database generation update and warmup. The synchronous request path also made broker loss, worker restart, partial Kubernetes failure, and concurrent clicks difficult to recover without either duplicate work or replacing the last known-good oracle too early.

## User-facing change

- Each downloaded database row now has an **Auto oracle** checkbox gated by **Auto warm**, exact AKS/Storage coordinates, update state, and write permission.
- The row keeps the current ready oracle visible while showing a separate active rebuild part count.
- Durable blocked, retry-pending, and exhausted states are visible. Exhaustion exposes an explicit **Retry** action after three automatic failures.
- Manual **Build Oracle** remains available and now dispatches the same bounded Celery task as automation.

## API and infrastructure summary

- Added authenticated `GET /api/warmup/oracle-preferences` and `PUT /api/warmup/oracle-preference` contracts with caller stamping, exact Auto warm dependency validation, current owner AKS/Storage write checks, and identity-redacted responses.
- Preferences are shared resource settings with opaque versions: enforced mode uses create-only first writes and ETag If-Match updates, so concurrent editors receive a conflict while an authorized fresh-version modifier can take over background responsibility.
- Preference GET uses indexed scope pagination; automatic reconciliation and retention advance independent durable 50-row cursors, removing the former global 500-row visibility/recovery cap.
- Added current/active/run/automation Blob documents protected by create-only claims, owner tokens, ETag compare-and-swap, immutable generation/layout identity, and current-ready publication.
- Added a dedicated 120-second reconcile-queue task, immediate targeted triggers after preference saves and successful warmups, a global cap of two builds, and a per-Storage cap of one build per pass.
- Added 5-minute, 30-minute, and 2-hour durable retry delays with third-failure exhaustion.
- Persisted task IDs before broker send, added delivery tokens and execution claims, recovered unclaimed deliveries after 120 seconds, and terminalized hard-crashed owners at a durable deadline.
- A delayed delivery from an already-published run cannot reset the automation state after a newer automatic run has been recorded, including after that newer run has failed and released its active claim.
- Durable oracle failure details are sanitised before Blob and JobState persistence so SAS material and full Azure GUIDs never reach the dashboard.
- BLAST submit records the selected `oracle_run_id` in JobState provenance and creates an immutable reverse reference before writing its pointer manifest.
- Added default-off 14-day retention with current/previous/active/reference protection, 50-run continuation pages, permanent GC tombstone handshakes, status-last deletion, result-purge reference cleanup, and strict run/blob caps.
- `ENFORCE_AUTO_ORACLE_RBAC=false`, `AUTO_ORACLE_RECONCILE_ENABLED=false`, and `AUTO_ORACLE_RETENTION_ENABLED=false` are wired through the shared Container App environment source for staged soak. The RBAC gate ships first and is enabled before execution; retention remains a separate final activation. No RBAC role assignment, network ACL, public Storage setting, or browser SAS path changed.

## Validation

- A corrected live `core_nt` order-oracle run completed all 10 AKS shards and published 10/10 parts in approximately one minute.
- `uv run pytest -q api/tests`: 5,450 passed, 4 skipped; this includes the owner/contributor/reader/dev-bypass Persona Matrix and all preference, route, retry, reconcile, state, dispatch, task, runtime, provenance, reference, retention, and environment contracts.
- `uv run ruff check api`: passed. Strict mypy checks passed for the 13 changed oracle service/task modules and both FastAPI route modules.
- `npm test -- --run`: 109 files and 982 tests passed. `npm run build` and zero-warning `npm run lint` also passed.
- Playwright mutation coverage for Auto oracle disable/save/refetch and exhausted Retry passed as part of the 51-passing safe browser suite. Integrated browser checks at 1440x900 and 390x844 showed the database modal, active/current part status, toggles, and actions without modal overflow or overlap.
- The restarted host-mode six-process stack registered the new Celery tasks, kept automatic execution dormant with the default-off gates, and passed the 27/27 API smoke suite.
- The documentation frontmatter guard checked all 62 navigated pages and `mkdocs build --strict` passed. `az bicep build --file infra/main.bicep --stdout` also passed.
- Twenty-eight post-implementation critique passes found no remaining Critical, High, or Medium defect. Residual Low observations are an intentionally generic retry-reset client error and non-enumerated internal blocker reason strings.
- Subscription-scope `what-if` could not run because the current Azure caller lacks `Microsoft.Resources/deployments/whatIf/action`; local Bicep compilation is the available infrastructure validation evidence.
