# Blast submit: default Execution Profile to a Running cluster

## Motivation

On `/blast/submit`, the Execution Profile cluster dropdown auto-selects the
first cluster only when the form has no persisted value. The sessionStorage
draft remembers the previously selected cluster name — so once the user has
visited the page, the dropdown sticks to that cluster across visits even
after it has been **Stopped**, while a healthy peer in the same fleet (e.g.
`heavy` Stopped vs `light` Running) is right there.

Researchers consistently expected a fresh "new search" to land on a Running
cluster.

## User-facing change

`/blast/submit` → "Execution Profile" → cluster picker:

- First mount with a non-empty cluster list now performs a one-shot default
  selection that prefers a workload-ready cluster (`power_state=Running` +
  `provisioning_state=Succeeded`) over the persisted draft when the persisted
  draft points to a Stopped/missing cluster.
- After that one-shot, manual picks in the dropdown are respected for the
  rest of the page lifetime, including picking a Stopped cluster on purpose.
- If only Stopped clusters exist, behaviour is unchanged (the first cluster
  is selected; the submit gate still blocks).

## API / IaC diff summary

None. Pure frontend logic change in
[web/src/pages/blastSubmit/useClusterSelection.ts](../../../web/src/pages/blastSubmit/useClusterSelection.ts).

## Validation evidence

- `cd web && npx tsc -p tsconfig.json --noEmit` — clean.
- `cd web && npx eslint src/pages/blastSubmit/useClusterSelection.ts` — clean.
- `cd web && npx vitest run src/pages/blastSubmit` — 140/140 passing.
