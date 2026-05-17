# BLAST Output Format 5 Option

## Motivation

The New Search form exposed output formats 0, 6, 7, and 11, but omitted BLAST XML output format 5 even though the backend supports outfmt 5 for sharded XML result merging. The form also kept the previous outfmt 7 default in session drafts, so users could keep seeing the old default after the code changed.

## User-facing change

The Algorithm Parameters output format selector now includes `5 - BLAST XML` and displays output formats sorted by numeric value: 0, 5, 6, 7, 11. New searches default to outfmt 5. When a user changes the output format, the setting is saved in browser `localStorage` under `elb-blast-outfmt` and restored on future visits. Session drafts no longer override that stored preference or the outfmt 5 default.

## API/IaC diff summary

No API or IaC change. This is a frontend-only update to the BLAST submit form model, Algorithm Parameters selector, and draft restoration logic.

## Validation evidence

- `cd web && npm run test -- src/pages/blastSubmit/useDraftForm.test.ts` — 6 tests passed.
- `cd web && npm run build`
- Browser check on `http://127.0.0.1:8090/blast/submit`: Output format options render as 0, 5, 6, 7, 11 with `5 - BLAST XML` present.
- Browser storage check: with no stored preference the Algorithm Parameters summary shows `Fmt: 5`; changing the selector to 6 writes `elb-blast-outfmt=6` and a reload restores `Fmt: 6`.
