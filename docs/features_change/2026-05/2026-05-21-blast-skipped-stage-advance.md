# BLAST Skipped Stage Advance

## Motivation

When node-local SSD warmup was already ready, the BLAST timeline could briefly
show the skipped Stage DB row as the current phase boundary instead of moving the
active highlight directly to Submit Job.

## User-facing change

Skipped timeline steps no longer hold the active phase pointer. A run that reuses
warm node-local SSD state now marks Stage DB as skipped and immediately advances
the active row to Submit Job.

## API/IaC diff summary

- Added a frontend step-state helper for skipped-step advancement.
- No API contract changes.
- No IaC changes.

## Validation evidence

- `npm run test -- src/components/BlastStepTimeline/stepState.test.ts`