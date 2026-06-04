---
title: Terminal paste safety, delete guard, onboarding, and honest capability tiers
description: Multi-line paste confirmation, an elastic-blast delete speed-bump, a first-run ephemeral-home warning, and an honest shipped/roadmap capability split in the terminal cockpit.
tags:
  - terminal
  - ui
  - security
---

# Terminal paste safety, delete guard, onboarding, and honest capability tiers

## Motivation

Phase 3 of the terminal-ergonomics work hardens the browser terminal against
two foot-guns (accidental multi-line paste, accidental cluster teardown), makes
the ephemeral nature of the terminal home directory obvious on first use, and
removes the inflated "live" capability count that overstated what the cockpit
actually ships today.

## User-facing change

- **Smart paste protection (#11):** pasting more than one line into the terminal
  (`Ctrl/Cmd+V`, `Ctrl/Cmd+Shift+V`, or the toolbar paste button) now opens a
  confirmation modal showing the line count and a preview before the text is
  sent to the shell. Single-line pastes are unaffected and go straight through.
- **`elastic-blast delete` guard (#12):** running `elastic-blast delete` or
  `elb delete` from the terminal is blocked with a message directing the user to
  the dashboard BLAST workflow, because that command tears down the cluster and
  all results. Global flags before the subcommand are still matched; `delete`
  appearing only inside a path argument is not.
- **First-run onboarding + ephemeral-home warning (#13):** the terminal login
  banner now shows a short "Get started" sequence (`az login`, `elb-cfg`
  scaffold, `elb-cfg --check`) and a prominent warning that `$HOME` is wiped on
  every revision restart, so inputs/outputs must be staged to Storage with
  `azcopy`.
- **Honest capability tiers (#14):** the cockpit "Innovation Coverage" panel no
  longer labels aspirational items as "live". Each capability is now either
  **shipped** (backed by a real cockpit panel, terminal guard, or CLI helper) or
  **roadmap** (designed but not yet wired to a user-reachable surface), and the
  count reads `N shipped · M roadmap`.

## Implementation

- [web/src/pages/RemoteTerminal.tsx](../../../web/src/pages/RemoteTerminal.tsx):
  `requestPaste` routes multi-line payloads through a `pendingPaste` confirmation
  modal; `confirmPendingPaste` / `cancelPendingPaste` resolve it. A native
  capture-phase `paste` listener intercepts `Ctrl+V`, and
  `attachCustomKeyEventHandler` covers `Ctrl/Cmd+Shift+V`.
- [web/src/pages/terminal/terminalCockpitModel.ts](../../../web/src/pages/terminal/terminalCockpitModel.ts):
  added `PasteAnalysis` + `analysePastePayload`; replaced the three-tier
  `status: "live" | "guarded" | "foundation"` field with a binary
  `tier: "shipped" | "roadmap"` (`CapabilityTier`) across all entries.
- [web/src/pages/terminal/TerminalCockpit.tsx](../../../web/src/pages/terminal/TerminalCockpit.tsx):
  count is now `shippedCount` / `roadmapCount`; `data-state` and the badge read
  `item.tier`.
- [web/src/theme/glass.css](../../../web/src/theme/glass.css): paste modal styles
  and `.terminal-cockpit__capability[data-state="shipped"|"roadmap"]` colours.
- [terminal/command_guard.sh](../../../terminal/command_guard.sh): added the
  `elastic-blast|elb … delete` block rule with a word-boundary, option-chain
  regex.
- [terminal/banner.sh](../../../terminal/banner.sh) and
  [terminal/motd](../../../terminal/motd): added `render_onboarding` and the
  mirrored plain-text onboarding + ephemeral-home warning.

## Validation

- `cd web && npm run build` — passes; `npx eslint` on the changed files — clean.
- `npx vitest run src/pages/terminal/terminalCockpitModel.test.ts` — 17 passed.
- `cd web && npm test -- --run` — 581 passed (68 files).
- `uv run pytest api/tests/test_terminal_command_guard.py -m ''` — guard block /
  allow cases pass (28 total).
- `uv run pytest -q api/tests` — 2599 passed, 3 skipped.
- `bash -n terminal/banner.sh` + rendered `render_compact_banner` shows the
  onboarding and ephemeral-home lines.
