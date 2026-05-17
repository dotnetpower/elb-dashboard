# Primer diagnostics in the submit form + degraded/truncated banner in analytics

**Date**: 2026-05-17
**Scope**: `web/src/pages/blastSubmit/`, `web/src/pages/BlastAnalytics.tsx`,
`web/src/api/blast.ts`, `web/src/pages/blastSubmit/configSerializer.ts`,
`web/src/pages/blastSubmit/fastaUtils.ts`

## Motivation

A molecular-diagnostics researcher pasting short oligos (PCR primers, qPCR
probes) into the BLAST submit form has no way to know — before running the
job — whether the design is sane: melting temperature, GC clamp, hairpin /
self-dimer risk. They discover problems only after the job runs and returns
junk. Likewise, when partial result downloads degrade the analytics summary,
the UI silently shows an empty / understated hit set with no indication that
the data is incomplete.

This change closes both gaps and adds defensive range-validation on
imported / duplicated job configs.

## User-facing changes

1. **Primer / probe diagnostics panel** in the query section (`blastSubmit`):
   when the program is nucleotide-class (`blastn` / `blastx` / `tblastx`) and
   any FASTA record is ≤ 50 nt, show a per-record row with:
   - Estimated Tm (Wallace rule ≤ 13 nt, salt-adjusted GC formula 14–60 nt;
     coloured green 55–65 °C, yellow 50–55 / 65–70, red outside).
   - GC%.
   - Longest internal G/C run (yellow if ≥ 4).
   - Hairpin stem-length warning (if ≥ 4 nt).
   - Self-dimer warning (if ≥ 4 nt complementary stretch).

2. **Degraded / partial results banner** in `BlastAnalytics`: when the
   aggregate response carries `degraded: true` or `truncated: true`, render a
   warning banner above the summary cards explaining the cause
   (`all_reads_failed`, `aggregation_failed`, `no_results`) and the
   `files_parsed / total_files / read_failures` counters.

3. **Config range validation**: importing a saved config or duplicating a
   stale job now drops out-of-range numeric fields before the form is
   re-populated:
   - `evalue` ∈ (0, 10]
   - `max_target_seqs` ∈ [1, 10_000]
   - `outfmt` ∈ [0, 18]
   A snapshot with `evalue = -1` previously round-tripped into the form and
   only failed at submit; now the bad value is silently dropped (form falls
   back to the initial default).

## Implementation notes

### `web/src/pages/blastSubmit/fastaUtils.ts`

Added pure helpers (no React deps, fully unit-testable):
- `meltingTemperatureC(seq)` — Wallace rule + salt-adjusted GC formula.
- `longestGcRun(seq)` — whitespace-tolerant.
- `findHairpin(seq, minStem = 4)` — reverse-complement search with a
  3-nt minimum loop gap, capped at 200 nt for O(n²) safety.
- `findSelfDimer(seq, minStem = 4)` — antiparallel sliding-window match
  against reverse complement, same 200-nt cap.
- `primerDiagnostics(seq)` — aggregator. Returns `null` for empty / non-
  nucleotide / >200 nt input.

### `web/src/pages/blastSubmit/QuerySection.tsx`

New `primerFindings` `useMemo` keyed on `(query_data, isNucleotideProgram)`.
Renders the `<PrimerDiagnosticsPanel>` only when at least one short oligo
is present. Display uses inline glass styling so it stays inside the
existing query section card.

### `web/src/pages/BlastAnalytics.tsx`

Added `<DegradedBanner>` with three known-reason labels (`all_reads_failed`,
`aggregation_failed`, `no_results`). Falls back to the raw `message` or
`degraded_reason` for unknown codes. Truncation explanation suggests
re-running with fewer query splits.

### `web/src/api/blast.ts`

Extended `resultsAggregate` response type with the optional `degraded`,
`degraded_reason`, `files_parsed`, `total_files`, `read_failures`,
`truncated` fields (backend was already sending them — see
`api/routes/stubs.py::blast_job_results_aggregate`).

### `web/src/pages/blastSubmit/configSerializer.ts`

Range guards added in both `partialFormFromJobPayload` (duplicate flow) and
`normaliseFormFields` (import flow). The guards drop the field instead of
clamping, so the user explicitly sees "default" rather than a silently-
mutated value.

## Validation

- Frontend: `cd web && npm test -- --run` → **152 passed** (was 131, +21
  new tests covering Tm, GC run, hairpin, self-dimer, primer diagnostics,
  and range validation in both serializer paths).
- Frontend build: `cd web && npm run build` → **built in 4.98 s**, no type
  errors.
- Backend: `uv run pytest -q api/tests` → **581 passed** (unchanged — this
  was a frontend-only change).

## Out of scope

- A server-side primer-design endpoint (`primerApi.design`) already exists
  but requires Primer3 backend wiring; this change only adds **client-side
  pre-flight diagnostics**, deliberately conservative so they cost nothing
  and never block submission.
- Hairpin / self-dimer detection uses exact Watson-Crick matching; we do
  not score energy (ΔG) — that would need a thermodynamic library.
