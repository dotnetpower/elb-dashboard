---
title: Finish the ProvisionModal split — identity + context fields under 600
description: >-
  Follow-up to the node-pool extraction: the AKS ProvisionModal's cluster
  identity fields and region/Resource Group context grid were moved into a
  ProvisionContextFields module, bringing the modal from 759 to 577 lines.
tags:
  - ui
  - contributor
---

# Finish the ProvisionModal split — identity + context fields under 600

## Motivation

Issue [#24](https://github.com/dotnetpower/elb-dashboard/issues/24) Priority 2
targets ~600 lines per file. The first ProvisionModal split (node-pool sections,
`2026-06-13-provisionmodal-pool-sections.md`) brought the modal from 1079 to 759
lines — below the original but still above target. This follow-up extracts the
remaining self-contained form blocks to land under 600.

## User-facing change

None. Pure structural refactor — the JSX (elements, inline styles, props) was
relocated verbatim, so the rendered DOM tree is byte-identical.

## What changed

- [web/src/components/cards/ClusterCard/ProvisionContextFields.tsx](../../../web/src/components/cards/ClusterCard/ProvisionContextFields.tsx)
  (269 lines) now exports two components:
  - `ProvisionIdentityFields` — the Cluster Name input (with its validity hint)
    and the optional classification `tier` selector.
  - `ProvisionContextFields` — the two-column Region + Resource Group grid
    (subscription-scoped region list with `AZURE_REGIONS` fallback; RG input
    with validity / exists / cross-RG advisory notes).
- [web/src/components/cards/ClusterCard/ProvisionModal.tsx](../../../web/src/components/cards/ClusterCard/ProvisionModal.tsx)
  (759 → 577 lines, **under the ~600 target**) composes the two new components
  and drops the now-unused `AZURE_REGIONS` / `CLUSTER_TIER_OPTIONS` /
  `panelChipStyle` imports. The public `<ProvisionModal>` prop contract is
  unchanged, so its caller (`ClusterCard` / `useClusterActions`) is untouched.

## Validation evidence

- `cd web && npm run build` — clean (tsc typecheck + vite bundle).
- `cd web && npx eslint <the 2 files>` — clean.
- `cd web && npx vitest run` — **823 passed** (93 files); no regression.
- Visual smoke: the dashboard root renders cleanly with no error boundary on the
  local host-mode dev server, confirming the ProvisionModal module graph (modal
  + the two extracted modules) imports and compiles. The modal itself requires a
  configured workspace / ARM access unavailable in the local network-blocked
  session, so its open-state render parity rests on the verbatim relocation +
  tsc/build + vitest.

## #24 status after this change

- `EndpointCard.tsx` — split, 388 lines (done).
- `ProvisionModal.tsx` — split, **577 lines (done, under target)**.
- Remaining Priority 2: `ClusterBento.tsx` (1112 lines) — separate scoped PR.
