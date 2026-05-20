# Cluster Plane AKS Card Density

## Motivation

The AKS card in Cluster Plane occupied too much vertical space when expanded, even though the same status, metrics, jobs, and action information still needed to remain visible.

## User-facing change

- Tightened the always-visible AKS pulse row by reducing padding and stat block width.
- Kept the expanded actions, metadata, jobs, and database/warmup extras, but reduced their internal padding, row gaps, and preview row height.
- Reflowed the metadata grid with smaller responsive cells so the same eight metrics consume less vertical space.
- Folded the idle Jobs empty-state prompt into the Jobs header line so clusters with no current jobs do not spend an extra row on empty copy.
- Capped the expanded Jobs roster height with internal scrolling so multiple job previews do not force the entire AKS card to grow.
- Moved the compact Add Cluster action from its own body row into the card header next to the status tag, preserving the action while removing another vertical row.

## API/IaC diff summary

No API or IaC changes. This is a frontend-only Cluster Plane density pass.

## Validation evidence

- `npm run build` in `web/` completed successfully on 2026-05-20.
- Browser verification at `http://127.0.0.1:8090/` confirmed the compact expanded AKS card still renders the cluster pulse surface, actions, metadata, jobs, and database extras.
- With live job rows loaded, the expanded card measured 390 px tall after the roster cap, down from the earlier 482 px measurement; the Jobs roster measured 150 px tall with internal scrolling.
- Browser verification confirmed the compact Add Cluster action renders inside the Cluster card header next to the OK status tag, with no separate body-row Add Cluster button above the cluster list.
