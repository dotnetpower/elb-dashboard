# AKS Card Action Alignment

## Motivation

The AKS card expanded action row mixed operational controls and the detail view action in a single left-flowing group. This made destructive or cost-affecting controls feel visually equivalent to opening the detail modal.

## User-facing change

`Open cluster detail` now sits on the left with accent styling and an info icon. Cluster power controls and `Delete` are grouped on the right, so `Stop` and `Delete` are visually separated from the detail action.

## API / IaC / deployment diff

- No API or IaC changes.
- Frontend `ClusterPulse` action row now has left and right action groups.
- The shared cluster action button supports an `accent` tone for the detail action.

## Validation

- `npm run build` - TypeScript + Vite production build succeeded.
- `npx eslint src/components/cards/ClusterPulse/atoms.tsx src/components/cards/ClusterPulse/PulseActions.tsx --max-warnings 0` - passed.