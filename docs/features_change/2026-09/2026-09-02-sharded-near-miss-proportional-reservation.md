---
title: Scale near-miss preservation for 5,000-hit shard merges
description: Replace the one-slot near-miss fallback with a candidate-proportional reservation so large tied shard fan-ins retain multiple lower-scoring variant candidates.
tags:
  - blast
  - user-guide
---

# Scale near-miss preservation for 5,000-hit shard merges

## Motivation

The first tied-cutoff mitigation reserved one lower-scoring subject. That closed
the minimal case but did not address large searches where
`max_target_seqs=5000`: individual shard outputs could contain many
lower-scoring, variant-bearing subjects, while a global score sort filled almost
all 5,000 final rows with the top tied class.

Exact [NCBI BLAST+](https://blast.ncbi.nlm.nih.gov/doc/blast-help/) membership
inside a large tied class requires the original internal selection order or a
same-snapshot strict oracle. Without that input, the merge now preserves the
candidate composition observed across the shard outputs instead of pretending
that strict global score truncation is equivalent to a single full-DB run.

## User-facing change

- Automatic mode counts unique subjects using their best-ranked hit, then
  reserves `ceil(N * L / (T + L))` slots, where `N` is
  `max_target_seqs`, `T` is the top-score subject count, and `L` is the
  lower-scoring subject count.
- Reservation still activates only when one score class fills the complete
  output window and also overflows it. At least one top hit is always retained.
- A 10,000-candidate regression with 6,000 tied top subjects, 4,000 lower-score
  variant subjects, and `max_target_seqs=5000` now returns 3,000 top subjects
  plus 2,000 variant subjects instead of 4,999 top subjects plus one variant.
- `ELB_DIVERSITY_AWARE_CUTOFF=0` restores strict top-N; `auto` selects the
  proportional policy; a positive integer requests a fixed reservation. A
  strict tie-order oracle continues to disable diversity reservation.
- The result notice reports both the number of reserved slots and the number of
  lower-scoring candidates available in the merged shard pool.
- Tabular merging now applies `max_target_seqs` to distinct subjects rather
  than HSP rows. Every HSP row belonging to a selected subject is retained, so
  additional alignment/variation segments are not discarded by the merge.

## API / IaC diff summary

- `merge-report.json` adds `diversity_candidate_count` and
  `diversity_reservation_mode`, plus explicit `*_subjects` and tabular
  `*_rows` counters. Existing tabular `*_hits` fields retain their historical
  HSP-row meaning for backward compatibility.
- Split-parent report aggregation sums candidate counts and carries a common
  mode, or reports `mixed` when child modes differ.
- The optional result `tie_cutoff` payload forwards the additive fields for the
  Descriptions notice. Older reports without them remain valid.
- Known multi-query sharded tabular submits now require a query identity field
  (`qseqid`, `qacc`, `qaccver`, or `qgi`) so rows from different queries cannot
  be merged into one group. Single-query custom layouts remain supported.
- No API request field, dependency, IaC, or Azure resource changes are required.

## Validation evidence

- `uv run pytest -q api/tests/test_sharded_merge.py -m ''` - 23 passed,
  including the 5,000-result regression and XML/tabular, fixed/off/oracle, and
  duplicate-subject/HSP boundaries.
- `uv run pytest -q api/tests/test_job_artifacts.py
  api/tests/test_blast_tasks.py::test_write_split_parent_result_artifacts_concats_child_gzip_and_report`
  - 41 passed.
- `uv run pytest -q api/tests/test_sharding_precision.py` - 43 passed, including
  the multi-query query-field gate.
- `uv run pytest -q api/tests -m ''` - 5,657 passed, 4 environment-dependent
  skips, including slow and subprocess-marked tests.
- `npm --prefix web test -- --run` - 990 passed; ESLint and the Vite production
  build passed.
- `ELB_TERMINAL_IMAGE=ncbi/blast:2.17.0
  scripts/dev/verify-local-blast-xml-sharding.sh` - the real BLAST+ full-DB and
  two-shard non-overflow baseline retained identical hit order and HSP tuples.
- A real BLAST+ 2.17.0 repetitive-query tabular probe produced `120` subjects
  and `720` HSP rows; merging with `max_target_seqs=100` retained exactly `100`
  subjects and all `600` HSP rows belonging to those selected subjects.
- An exploratory 10,000-subject real-BLAST probe was not usable as a
  top/lower-score membership oracle: the repetitive query produced 30,000
  perfect HSP rows and no variant-subject rows. It nevertheless exposed the
  subject-versus-HSP-row limit distinction fixed above.
- Ruff, shell syntax, docs frontmatter, and MkDocs strict build passed.
- Host-mode browser validation at desktop and `390x844` rendered
  `20 slots from 80 lower-scoring candidates`; the near-miss row remained
  visible and mobile document width stayed within the viewport.

The deployed API listed 89 recent dashboard jobs but none recorded
`max_target_seqs=5000`; the sibling OpenAPI list timed out, and local Storage
data-plane reads were correctly blocked by private-network rules. No network
ACL was changed. The supplied production job artifacts were therefore not
available for direct replay; the 5,000-result failure is covered by the
deterministic 10,000-candidate regression fixture.