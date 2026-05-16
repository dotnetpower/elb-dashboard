# Dashboard header layout — consistent breadcrumb + title + right-side actions

## Motivation

Other primary pages (New Search, Custom DB, Lab Tools) all share the same
header pattern:

1. Breadcrumb on top.
2. An icon + title on the left, with a one-line description underneath.
3. Context-sensitive controls (workspace pill, readiness dots, refresh
   indicator, action buttons) on the right.

The Dashboard, ironically the landing page, did not follow this pattern. Its
header was a plain title + description block with no icon, no right-side
controls, and the breadcrumb was hidden because `Breadcrumb` returned `null`
for `pathname === "/"`. That made the rest of the app feel "dressed up"
relative to the home page.

User request:

> New Search, Custom DB, Lab Tools 처럼 브러드크럼과 제목, 그리고 필요한
> 기능이 우측에 보이는것 처럼 대시보드도 그렇게 나오도록하자

## User-facing change

* The Dashboard now renders a `Dashboard` breadcrumb crumb (non-clickable —
  it *is* the current page) so the header skeleton matches every other page.
* The `page-header` element now uses the standard flex layout:
  * **Left**: `LayoutGrid` icon + `Dashboard` title + a sharper one-line
    description ("Your BLAST workspace at a glance — clusters, registries,
    storage, and terminal health, polled live.").
  * **Right**: a workspace context pill (`storage account · region`), an
    auto-refresh badge ("Auto-refresh 30s") replacing the inline grey text
    that used to sit under the title, a `Settings` button (mirrors the
    `ConfigBar` gear so the action is reachable from the header itself),
    and a `Getting started` button that re-opens the dismissed checklist.
* The Getting Started checklist had no in-page way to be re-opened once
  dismissed (a session-storage flag suppressed it). The new `Getting
  started` button removes the flag and re-shows the checklist immediately,
  which previously required a hard refresh + new session.

No behaviour of the existing cards (Cluster / ACR / Storage / Terminal /
Sidecars / Jobs) changes. `ConfigBar` is unchanged — the redundant
`Settings` button in the header is intentional for parity with how Lab Tools
exposes its primary control next to the title.

## API / IaC diff summary

None. Frontend-only change.

## Files touched

* `web/src/components/Breadcrumb.tsx` — render `Dashboard` crumb on
  `pathname === "/"` instead of returning `null`.
* `web/src/pages/Dashboard.tsx` — new `<header className="page-header">`
  block matching the Lab Tools / Custom DB / New Search shape; lucide icons
  added (`LayoutGrid`, `RefreshCw`, `Settings`, `HelpCircle`, `Database`).

## Validation evidence

* `cd web && npx tsc --noEmit` — clean.
* `cd web && npm run build` — `built in 5.27s`, no errors. The pre-existing
  bundle-size warning about `index-*.js > 500 kB` is unchanged (not caused
  by this PR).
* Visual: dashboard at `http://127.0.0.1:18080/` now shows
  `Dashboard` breadcrumb → `LayoutGrid` icon + title on the left, and
  `<storage> · <region>` pill, `Auto-refresh 30s` badge, `Settings` button,
  and (after dismissal) `Getting started` button on the right.
