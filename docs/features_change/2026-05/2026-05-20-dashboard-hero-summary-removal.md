# Dashboard Hero Summary Removal

## Motivation

The dashboard header repeated readiness, workspace, cluster, image, and terminal status that are already represented by the main dashboard cards. The duplicated row added visual noise and made the first viewport feel busier without giving operators a new action or diagnosis path.

## User-facing change

The dashboard header now keeps only the page title, subtitle, subscription/workload selectors, auto-refresh chip, Getting Started shortcut, and settings button. The redundant readiness summary row is removed.

## API / IaC diff summary

None. Frontend-only UI cleanup.

## Validation evidence

- `cd web && npm run build` succeeded after formatting the touched files.
