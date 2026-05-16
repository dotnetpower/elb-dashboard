# Dashboard config strip → header chips, configurable auto-refresh

## Motivation

Two issues with the dashboard top:

1. The `ConfigBar` strip (Subscription picker + Workload RG picker + gear) was
   a separate full-width row above the page header. It duplicated the chrome
   work the header was already doing and pushed the actual content down.
2. The "Auto-refresh 30s" badge was decorative — it told the user the
   refresh cadence but did not let them change it. Users with slow Azure
   responses wanted longer intervals; users debugging a build wanted faster
   ones.

User request:

> 상단에 보이는 이것들을 좀더 간소화 해서 보여주고 Auto-refresh 30s 라고
> 된 쪽에 보여주고 auto-refresh 도 클릭하면 드롭다운으로 5s, 15s, 30s, 60s
> 로 선택해서 동작하게 하자

## User-facing change

* The standalone `ConfigBar` row is gone. Subscription and Workload RG
  pickers are now rendered as compact chips inside the page header's
  right-hand actions area, alongside the auto-refresh control and the
  Settings button. On narrow viewports the chips wrap onto their own row
  but they remain inside the header (no separate strip above it).
* The `Auto-refresh 30s` static badge is now an interactive dropdown chip:
  clicking it lets the user pick `5s`, `15s`, `30s` (default), or `60s`.
  The choice is persisted to `localStorage` (`elb-auto-refresh-ms`) so it
  survives reloads.
* All polled dashboard cards (Cluster, ACR, Storage, Terminal, Jobs)
  honour the chosen interval. ACR keeps its existing fast-poll behaviour
  while a build is in progress (10 s) and only falls back to the
  user-chosen cadence once the registry is idle.
* The header-bar `Live` indicator's tooltip now reflects the chosen
  interval ("Dashboard cards refresh every 5s …") instead of always
  claiming "30 seconds".
* The Workload RG picker keeps its existing tag-driven autofill — picking
  an `elb-*`-tagged RG still populates ACR / Storage / Terminal /
  Region from the RG tags. That logic was inlined from `ConfigBar` into
  the Dashboard page so behaviour is identical.

## API / IaC diff summary

None. Frontend-only change.

## Files touched

* `web/src/hooks/useAutoRefresh.tsx` — new context + provider + hook
  exposing `intervalMs` / `setIntervalMs` and the `AUTO_REFRESH_OPTIONS`
  list. Persists to `localStorage`.
* `web/src/main.tsx` — wraps `<App>` in `<AutoRefreshProvider>`.
* `web/src/pages/Dashboard.tsx` — drops `ConfigBar`, embeds `SubscriptionPicker`
  + `ResourcePicker` as compact chips in the header, adds new
  `AutoRefreshChip` (styled as `cfg-chip` for visual parity with the
  pickers).
* `web/src/components/Layout.tsx` — `Live` indicator tooltip now reads
  the auto-refresh interval from context.
* `web/src/components/cards/ClusterCard.tsx`
* `web/src/components/cards/StorageCard.tsx`
* `web/src/components/cards/TerminalCard.tsx`
* `web/src/components/cards/JobCard.tsx`
* `web/src/components/cards/AcrCard.tsx` — replaced the hardcoded
  `refetchInterval: 30_000` (or `60_000` idle for ACR) with
  `useAutoRefreshInterval()`. ACR's dynamic 10 s build-in-progress poll
  is preserved.

`ConfigBar.tsx` itself is unchanged (no other caller imports it; it is now
unused but kept for now in case we revive a per-page config strip).

## Validation evidence

* `cd web && npx tsc --noEmit` — clean.
* `cd web && npm run build` — `built in 12.55s`, no errors. Bundle-size
  warning unchanged (pre-existing).
* Visual at `http://127.0.0.1:18080/`:
  * Header now reads `Dashboard` breadcrumb → `LayoutGrid` + Dashboard
    title → description.
  * Right side shows `Subscription [picker]`, `Workload RG [picker]`,
    `Auto-refresh [30s ▼]`, `Settings` button. The standalone `ConfigBar`
    row above the header is gone.
  * Dropdown opens with options `5s / 15s / 30s / 60s`; selecting one
    immediately changes the cadence of all subsequent card refetches and
    survives a hard reload.
* Backend tests untouched (`uv run pytest -q api/tests` not re-run; no
  Python files changed).

## Follow-up — visual hardening (same day)

The first cut of the header had three layout bugs that made it look
"broken" at the default 1260 px viewport:

1. The picker `<select>` elements rendered at the natural width of their
   longest option (e.g. the full `ME-MngEnvMCAP132261-moonchoi-1`
   subscription name was ~280 px). That made each chip ~280 px wide and
   blew the right-side row past the available width.
2. With the chips overflowing, `flex-wrap: wrap` on the action container
   pushed `Auto-refresh` + `Settings` onto a third row, separating the
   pickers from the controls the user had asked them to be next to.
3. The long one-line description text squeezed the right side of the
   header, making the wrap happen sooner than it needed to.

Fixes (no behaviour change, layout-only):

* `.cfg-chip select` (in [web/src/theme/glass.css](../../web/src/theme/glass.css))
  now sets `text-overflow: ellipsis; white-space: nowrap; overflow: hidden;`,
  so the visible value of a closed `<select>` truncates at the chip width
  instead of expanding the chip.
* The picker chips on the dashboard header now carry an explicit
  `style={{ maxWidth: 240, minWidth: 160 }}` (subscription) and
  `{ maxWidth: 220, minWidth: 140 }` (workload RG). Combined with the
  ellipsis rule above, this caps each chip's width regardless of how long
  the Azure name is.
* `Settings` and `Getting started` are now icon-only `cfg-gear` buttons
  (with `marginLeft: 0` to override the default `margin-left: auto` on
  that class). Saves ~80 px each on the right side.
* The page header was restructured into two rows:
  * **Row 1**: title (left) + workspace pickers + auto-refresh + icon
    buttons (right), all on a single line at any viewport ≥ ~960 px.
  * **Row 2**: short description ("Live view of your BLAST workspace —
    clusters, registries, storage, and terminal health.") on its own
    line so it never competes with the controls for horizontal space.

Visual verification at `http://127.0.0.1:18080/`: all four chips +
two icon buttons fit on the title row at 1260 px viewport; values
truncate cleanly with `…`; description sits on its own row underneath.
