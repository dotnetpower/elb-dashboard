# Dashboard BLAST job row metadata

## Motivation

The Dashboard AKS card and BLAST Jobs card should make each search identifiable without opening the full Recent searches page.

## User-facing change

Dashboard BLAST job rows now follow the Recent searches row shape: title on the first line, then BLAST algorithm, database, query label, and cluster chip on the metadata line.
The Dashboard BLAST Jobs card now keeps newest jobs first instead of reversing the API order, so rows show titles such as `20260520 MPXV F3L NC_003310 strict Web oracle patched finalizer` before older fallback-titled jobs.
The AKS card active-job preview uses the same status + title + metadata shape; the old leading short job ID column was removed.

## API / IaC diff summary

- Extended the frontend job row view model with `program` and `clusterName`.
- Split shared job row types into `ClusterBento/jobTypes.ts` and shared title/meta rendering into `BlastJobIdentity`.
- Updated the AKS active jobs preview and Dashboard BLAST Jobs card rendering.
- Fixed Dashboard BLAST Jobs ordering to sort by creation time descending while keeping active jobs ahead of completed/failed jobs.
- Aligned the AKS active jobs preview row layout with the Dashboard BLAST Jobs row layout.
- No backend, API, or IaC changes.

## Validation evidence

- `cd web && npm run build` — passed. Vite reported the existing large chunk warning.
- Browser check on `http://localhost:8090/` confirmed Dashboard BLAST Jobs rows render as title plus `blastn · core_nt`, query, and cluster chip metadata.
- Browser check confirmed the first Dashboard BLAST Jobs row is `20260520 MPXV F3L NC_003310 strict Web oracle patched finalizer`.
- After the SRP refactor, `cd web && npm run build` still passed and a browser DOM check confirmed the Dashboard BLAST Jobs rows still render the expected title and metadata.
- Direct browser check found the current AKS card has `Active jobs · 0`, so no active-job row is present to screenshot locally; the rendered code path was updated and covered by the frontend build.
