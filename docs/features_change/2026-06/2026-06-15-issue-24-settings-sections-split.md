---
title: "Issue #24 — split the four oversized SettingsPanel section files"
description: "Bring VnetPeeringSection, DiagnosticsSection, PublicHttpsSection, and TelemetrySection under the ~600-line SRP threshold by extracting their data-orchestration hook, identity-detail view, and pure helper modules, completing issue #24."
tags:
  - contributor
  - ui
---

# Issue #24 — final four SettingsPanel sections split

## Motivation

Issue #24 (split oversized files violating SRP) acceptance criterion #1 requires
no `SettingsPanel` section to exceed ~600 lines. After the earlier `SettingsPanel.tsx`
decomposition four section files were still over: `VnetPeeringSection` (749),
`DiagnosticsSection` (745), `PublicHttpsSection` (663), `TelemetrySection` (626).
This change splits all four — the last remaining work on issue #24.

## User-facing change

None. Pure structural refactor across all four sections — no behaviour, no API
contract change. Live render output is unchanged.

## Diff summary

- **`VnetPeeringSection.tsx`** (749 → 333): all state + the five data effects
  (ARM subscription→RG→VNet cascade, AKS discovery, live elb-openapi internal-LB
  IP auto-detect), the peer+probe round-trip, the RBAC copy/retry loop, the NSG
  rule preview/apply flow, and the existing-peerings housekeeping moved into a new
  **`vnetPeering/useVnetPeering.ts`** (551) hook. The section now calls the hook,
  destructures the model, and renders only.
- **`DiagnosticsSection.tsx`** (745 → 204): the `IdentitySecurityDetail` view + its
  four cards (`DiagnosticDetailHeader`, `DashboardIdentityCard`,
  `SignedInAccountCard`, `RgAccessCard`) + the `RgTarget` / `SignedInIdentity` types
  and `SCOPE_LEVEL_LABEL` moved into a new **`IdentitySecurityDetail.tsx`** (562).
  `DiagnosticsSection.tsx` keeps only the category launcher and **re-exports**
  `IdentitySecurityDetail` so the existing `@/components/settings/sections/DiagnosticsSection`
  import path (used by the diagnostics page) is unchanged.
- **`PublicHttpsSection.tsx`** (663 → 582): the pure helpers — Let's Encrypt
  contact-email gate (`isPublicLetsEncryptEmail` + the mutable private-use TLD set
  and its `setPrivateUseTlds` setter), the ordered setup-phase metadata
  (`PUBLIC_HTTPS_PHASES` / `lookupPublicHttpsPhase`), and `formatElapsedSeconds` —
  moved into a new **`publicHttpsHelpers.ts`** (109). The runtime-used setter was
  renamed from `_setPrivateUseTldsForTesting` to `setPrivateUseTlds`.
- **`TelemetrySection.tsx`** (626 → 564): the pure presentational helpers
  (`isWellFormedConnectionString`, `extractInstrumentationKeyTail`,
  `describeEffectiveSource`, `appInsightsPortalUrl`) moved into a new
  **`telemetryHelpers.tsx`** (82) — `.tsx` because the source-badge descriptor
  returns a small status icon.

No external consumer imported any moved symbol except `IdentitySecurityDetail`
(preserved via re-export). No other file changed.

## Validation evidence

- `cd web && npm run build` → built (tsc -b clean; pre-existing chunk-size warning
  only).
- `npx eslint src/components/settings` → clean (exit 0); no unused-import survivors.
- `npx vitest run` → **895 passed (99 files)** — full frontend suite green,
  including the `peeringHealth` / `dismissedPeerings` settings tests and
  `useSettingsPanel`.

## Issue #24 status

All acceptance criteria now met:
- SettingsPanel sections — every file ≤ ~582 lines.
- `prepare_db.py` cloud/data-plane logic extracted (commit 1e6266c).
- `web/src/pages/mockups/` deleted.
- Priority 2 frontend (`EndpointCard`, `ProvisionModal`, `blast.ts`,
  `ClusterBento.tsx`) — all split.
- `pytest` / `npm run build` green after each split.

Issue #24 can be closed.
