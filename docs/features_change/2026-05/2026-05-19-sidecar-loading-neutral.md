# Sidecar Loading Neutral Chrome

## Motivation

The Sidecars card used `down` placeholders before the first metrics snapshot arrived. That made each sidecar node briefly render with a red Down border while data was still loading.

## User-facing change

Sidecar nodes now render with neutral gray borders and dots until their own metric entry is loaded. Once the snapshot contains that sidecar, the node changes to the Healthy, Degraded, or Down color.

## API / IaC / deployment diff

- No API or IaC changes.
- Frontend topology nodes now receive an explicit per-sidecar loading state instead of deriving pre-load styling from the Down placeholder.
- Link degradation styling waits until the relevant sidecar metric is loaded.

## Validation

- `npm run build` - TypeScript + Vite production build succeeded.
- `npx eslint src/components/cards/SidecarsCard/StatusDot.tsx src/components/cards/SidecarsCard/TopoNode.tsx src/components/cards/SidecarsCard/SidecarsCard.tsx --max-warnings 0` - passed.