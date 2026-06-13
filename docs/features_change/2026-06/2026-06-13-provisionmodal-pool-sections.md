---
title: Extract the node-pool sections out of ProvisionModal
description: >-
  The AKS ProvisionModal's Workload and System node-pool form sections (plus
  their shared glass-panel styles) were moved into a sibling
  ProvisionPoolSections module, dropping the modal from 1079 to 759 lines.
tags:
  - ui
  - contributor
---

# Extract the node-pool sections out of ProvisionModal

## Motivation

Issue [#24](https://github.com/dotnetpower/elb-dashboard/issues/24) Priority 2
flags `web/src/components/cards/ClusterCard/ProvisionModal.tsx` (1079 lines) as
mixing modal UI, form state, and provisioning orchestration. The modal is
already a controlled component (all form state lives in the parent
`useClusterProvisioning` hook and is threaded in via props), so the two
node-pool `<section>` panels are cleanly separable.

## User-facing change

None. Pure structural refactor — the JSX (elements, inline styles, props) was
relocated verbatim, so the rendered DOM tree is byte-identical.

## What changed

- New [web/src/components/cards/ClusterCard/ProvisionPoolSections.tsx](../../../web/src/components/cards/ClusterCard/ProvisionPoolSections.tsx)
  (407 lines) — exports `WorkloadPoolSection` and `SystemPoolSection`, plus the
  shared glass-panel style tokens they use. `panelChipStyle` is re-exported
  because the modal's Resource Group block reuses the same chip token. The
  components receive form values + setters via explicit props; no state moved.
- [web/src/components/cards/ClusterCard/ProvisionModal.tsx](../../../web/src/components/cards/ClusterCard/ProvisionModal.tsx)
  (1079 → 759 lines) composes the two new sections and imports
  `panelChipStyle`. The now-unused inline panel-style constants and the icon /
  helper imports they needed (`Cpu`, `Settings2`, `ChevronDown`,
  `ChevronRight`, `describeAksSku`, `formatAksSkuOption`, `CSSProperties`,
  `MAX_SYSTEM_NODE_COUNT`) were removed from the modal. The public
  `<ProvisionModal>` prop contract is unchanged, so its caller
  (`useClusterActions` / `ClusterCard`) is untouched.

## Validation evidence

- `cd web && npm run build` — clean (tsc typecheck + vite bundle); all prop
  wiring type-checks.
- `cd web && npx eslint <the 2 files>` — clean.
- `cd web && npx vitest run` — **823 passed** (93 files); no regression.
- The modal itself cannot be opened on the local network-blocked dev session
  (it requires a configured cluster context / ARM access), so a populated
  visual smoke is not possible locally; the verbatim relocation + tsc/build +
  vitest evidence covers the render parity.

## Scope note (partial — remaining ProvisionModal work)

This extracts the two node-pool sections (the largest cohesive blocks). The
modal is now 759 lines — below the original 1079 but still above the ~600 SRP
target. The remaining cohesive blocks (region/RG context grid, preflight
checklist wiring, live-provisioning status panel, error card, and the
cost/actions footer) are left for a follow-up scoped PR per the #24 note that
these component splits are "separate scoped PRs needing browser/visual
validation".
