# Query Example Modal

## Motivation

The BLAST New Search form had a single hard-coded `Load example` action. The sibling ElasticBLAST benchmark repository already carries several private benchmark FASTA examples, so the submit form should let users choose from those templates instead of loading one fixed sequence.

## User-facing change

`Load example` now opens a modal with bundled FASTA templates from the benchmark sample set. Each card shows the organism/family, label, source description, sequence count, and length. Selecting a card loads the FASTA into the query textarea, switches the program to `blastn`, and fills the job title when it is empty.

The initial template set includes Monkeypox F3L, Plasmodium falciparum 18S rRNA, and SARS-CoV-2 N/RdRP examples from `~/dev/elastic-blast-azure/benchmark/private/260420_elastic_blast_test_fasta_file_10ea`.

## API/IaC diff summary

No API or IaC change. This is a frontend-only submit form update with bundled template data and modal styling.

## Validation evidence

Pending local validation:

- `cd web && npm run test -- src/pages/blastSubmit/queryExamples.test.ts src/pages/blastSubmit/shardingAvailability.test.ts src/pages/blastSubmit/useDraftForm.test.ts`
- `cd web && npm run build`
- Browser check on `http://127.0.0.1:8090/blast/submit`: `Load example` opens the modal, selecting a template populates the query textarea and closes the modal.
