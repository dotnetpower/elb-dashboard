# Microsoft brand palette (opt-in, light theme only)

## Motivation
After the vividness pass made the Aurora light theme more readable, the
user asked to try a Microsoft brand-colour palette as an alternative
look — with the explicit requirement that it must be revertible without
shipping a config edit.

## User-facing change
- New light-theme palette **"Microsoft brand"** (Blue #0078D4 family),
  selectable from the topbar via a Palette icon that appears only in
  light mode. Choice persists in `localStorage` under `elb-palette`.
- Default remains the existing Aurora (lavender/cobalt) palette.
  First-time visitors see the Aurora look exactly as today.
- Toggling palette is one click and instant (no reload).
- Dark mode ignores `data-palette` entirely — the new attribute has no
  effect outside `[data-theme="light"]`.

### Token mapping for `data-palette="msbrand"`
| Token | Aurora value | Microsoft brand value |
|-------|--------------|-----------------------|
| `--accent` | `#4b5cf5` | `#0078d4` (Blue) |
| `--success` | `#047857` | `#07641d` (Dark Green) |
| `--warning` | `#b45309` | `#73391d` (Dark Orange) |
| `--danger` | `#b91c1c` | `#73262f` (Dark Red) |
| `--purple` | `#6c5be0` | `#8661c5` (Purple) |
| `--teal` | `#0f766e` | `#225b62` (Dark Teal) |
| `--bg-canvas` | `#eef1f8` | `#f4f3f5` (Off White) |
| `--text-primary` | `#0e1230` | `#1b1b1b` |
| `--text-muted` | `#353c66` | `#454142` (Dark Gray) |
| Aurora wash | radial lavender + cobalt | flat neutral `#fafafb → #ececef` |
| Glass blur | `16px` | `8px` (sober, less frost) |

Per-card accent identity preserved (cluster=Blue, storage=Dark Green,
acr=Purple, terminal=Dark Teal, jobs=Dark Orange) so the header bands
still tell you at a glance which card is which.

## API / IaC diff summary
None. Frontend-only.

## Files touched
- [web/src/hooks/usePalette.ts](../../../web/src/hooks/usePalette.ts) — new hook
  that mirrors `useTheme` but writes `data-palette` and persists under
  `elb-palette`.
- [web/src/components/Layout.tsx](../../../web/src/components/Layout.tsx) — light-mode-only
  Palette toggle button next to the theme toggle (lucide `Palette` icon).
- [web/src/theme/glass.css](../../../web/src/theme/glass.css) — `[data-theme="light"][data-palette="msbrand"]`
  override block. All rules are scoped to the compound selector so the
  Aurora palette and dark mode are provably untouched.

## Validation
- `npx tsc --noEmit` clean.
- Playwright: loaded `/` with `elb-theme=light`, set `elb-palette=msbrand`
  → accent computed to `#0078d4`, header bands rendered with MS Blue /
  Dark Green / Purple / Dark Teal / Dark Orange tints, primary buttons
  switched to solid Microsoft blue, screenshots captured for Dashboard
  cards (AKS / ACR / Storage / Terminal / Sidecars).
- Clicked the Palette toggle → `data-palette` flipped to `aurora`,
  `--accent` reverted to `#4b5cf5`, button title updated to
  "Try Microsoft brand palette", localStorage persisted the new value.
- Dark mode rendered with palette stored as `msbrand` is unchanged —
  the override selector requires `[data-theme="light"]`.

## Reverting
- Single click of the topbar Palette icon (light mode) restores Aurora.
- Programmatic revert: `localStorage.removeItem("elb-palette")` →
  next load defaults back to Aurora.
- Code revert: deleting the
  `[data-theme="light"][data-palette="msbrand"]` block in
  [web/src/theme/glass.css](../../../web/src/theme/glass.css) and the
  Palette button in
  [web/src/components/Layout.tsx](../../../web/src/components/Layout.tsx)
  removes the variant entirely; the `usePalette` hook becomes a no-op.
