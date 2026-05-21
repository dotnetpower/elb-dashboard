# Dashboard AKS jobs preview limit

## Motivation

The expanded AKS card jobs roster could grow enough to introduce a nested scrollbar, which made the dashboard feel heavier than necessary for a quick monitoring surface.

## User-facing change

The AKS card now shows only the latest three jobs in the inline roster. The nested jobs scrollbar is removed, and the overflow affordance is a more visible `More jobs` button that opens the full Recent searches page filtered to the cluster.

## API / IaC diff summary

No API or IaC changes. The frontend cluster pulse preview limit changed from four rows to three, and the jobs preview CTA styling was updated.

## Validation evidence

- `cd web && npm run build`