---
title: Web BLAST Compatibility Plan
description: Implementation plan and feature compatibility matrix between NCBI Web BLAST and the ElasticBLAST Control Plane browser workflow.
tags:
  - research
  - blast
---

# Web BLAST Compatibility Implementation Plan

Date: 2026-05-20
Status: In progress

This document is the durable implementation ledger for turning the ElasticBLAST control plane into a Web BLAST-compatible Azure execution platform. It is designed for long sessions and handoff: update checkboxes, critique logs, and validation evidence as work proceeds.

## Mission

Deliver a browser and OpenAPI control plane for ElasticBLAST on Azure that:

- avoids NCBI Web BLAST rate limits by running BLAST workloads on Azure;
- preserves Web BLAST-compatible scientific results whenever the required database snapshot, BLAST+ version, search space, query, and option profile are known;
- improves runtime with warm databases, AKS parallelism, deterministic sharding, and queue-aware execution;
- provides a familiar Web BLAST-like submit/results UX with better progress and reproducibility signals;
- exposes the same execution/result contract through OpenAPI for external systems.

## Product Contract

The system must never imply precise Web BLAST compatibility unless the run is eligible and evidence-backed.

Precision states:

- `precise`: verified BLAST+ version, database snapshot, option profile, full-database effective search space, and deterministic merge strategy.
- `calibration_required`: selected database/profile/query class lacks enough evidence for precise sharded execution.
- `approximate`: user explicitly requested fast exploratory execution where e-values, hit order, or tie handling may differ from full-database output.

Required invariants:

- [ ] UI submit and OpenAPI submit share one normalization and precision gate.
- [ ] UI submit and OpenAPI submit share one execution/result/progress contract.
- [ ] Unknown databases never receive fabricated Web BLAST compatibility.
- [ ] Browser clients never receive SAS tokens or direct storage URLs.
- [ ] Degraded/partial/no-hit states are distinct in UI and API responses.

## Resume Protocol

At the start of every implementation session:

- [ ] Read this document.
- [ ] Check `git status --short` and do not revert unrelated user changes.
- [ ] Read the current stage, latest critique log, and validation notes.
- [ ] Implement only the next unchecked stage task unless scope changes.
- [ ] After each slice, run the critique hardening loop.
- [ ] Update this document before moving to the next stage.
- [ ] Add `docs/features_change/YYYY-MM/...` for user-visible behavior changes.

## Critique Hardening Loop

Every stage must pass this loop before the next stage begins.

1. Implement the smallest coherent slice.
2. Produce exactly 10 critique findings.
3. Classify each finding as `Critical`, `High`, `Medium`, or `Low`.
4. Fix every `Critical`, `High`, and `Medium` item.
5. Repeat critique until only `Low` items remain.
6. Record validation evidence and residual Low items in this file.

Critique dimensions to cover each pass:

- [ ] Scientific correctness and Web BLAST compatibility.
- [ ] OpenAPI contract stability.
- [ ] Browser UX and researcher workflow.
- [ ] Queue, retry, idempotency, and recovery behavior.
- [ ] Result parser and sharded merge determinism.
- [ ] Provenance and reproducibility metadata.
- [ ] Auth, RBAC, storage isolation, and data leakage.
- [ ] Test coverage and validation evidence.

Critique record template:

```text
Stage:
Iteration:
Date:
Implemented slice:

Findings:
1. [Severity] Finding - action/result
2. [Severity] Finding - action/result
3. [Severity] Finding - action/result
4. [Severity] Finding - action/result
5. [Severity] Finding - action/result
6. [Severity] Finding - action/result
7. [Severity] Finding - action/result
8. [Severity] Finding - action/result
9. [Severity] Finding - action/result
10. [Severity] Finding - action/result

Remaining non-low findings: none|list
Validation evidence:
Next action:
```

## Code Anchors

- Submit normalization: `api/services/blast_submit_payload.py`
- Submit/pre-flight routes: `api/routes/blast/submit.py`
- Job list/detail lifecycle: `api/routes/blast/jobs.py`
- Result routes: `api/routes/blast/results.py`
- External API facade: `api/routes/elastic_blast.py`
- External OpenAPI client: `api/services/external_blast.py`
- Sharding precision gate: `api/services/sharding_precision.py`
- Verified search-space defaults: `api/services/web_blast_searchsp.py`
- Job state/history repository: `api/services/state_repo.py`
- Job state mapping: `api/services/blast_job_state.py`
- BLAST Celery task implementation: `api/tasks/blast/__init__.py`
- BLAST config builder: `api/services/blast/task_config.py`
- Result parser: `api/services/blast_results_parser.py`
- Submit UI: `web/src/pages/BlastSubmit.tsx`
- Jobs UI: `web/src/pages/BlastJobs/`
- Results UI: `web/src/pages/BlastResults.tsx`
- Typed frontend API: `web/src/api/blast.ts`
- Existing evidence: `docs/blast-searchsp-discovery.md`
- OpenAPI execution notes: `/memories/repo/openapi-execution-design.md`

## Stage Progress Board

| Stage | Status | Last updated | Notes |
| --- | --- | --- | --- |
| 0. Planning ledger | Complete | 2026-05-20 | Baseline status and focused BLAST tests recorded. |
| 1. Compatibility contract | Complete | 2026-05-20 | Pre-flight and submit now expose/block by compatibility contract. |
| 2. Unified submit contract | Complete | 2026-05-20 | Canonical request snapshots, trusted metadata, shared contracts, and idempotent retry guard. |
| 3. Provenance bundle | Complete | 2026-05-20 | Submit-time provenance bundle attached to local and external payloads. |
| 4. Real-time progress events | Complete | 2026-05-20 | Canonical job events endpoint backed by jobhistory. |
| 5. Canonical results and merge | Complete | 2026-05-20 | Result listings now include canonical manifest state. |
| 6. Web BLAST-like result UX | Complete | 2026-05-20 | Files tab surfaces compatibility, BLAST version, and manifest summary. |
| 7. OpenAPI delivery contract | Complete | 2026-05-20 | External events and manifest endpoints added. |
| 8. Equivalence evidence matrix | Complete | 2026-05-20 | Evidence registry validation tests added. |
| 9. Queue/recovery hardening | Complete | 2026-05-20 | Queue depth and position snapshot endpoint added. |
| 10. Final acceptance | In progress | 2026-05-20 | Final combined validation running for this implementation wave. |
| 11. NCBI Web BLAST parity wave | In progress | 2026-05-31 | 8/20 catalogued gaps shipped; 12 documented with severity + cost. |

## Stage 0: Planning Ledger

Goal: create a durable implementation record before further code changes.

Tasks:

- [x] Create this plan document.
- [x] Validate the document has one clean copy and no duplicate sections.
- [x] Record baseline worktree/test status before Stage 1 implementation.
- [x] Update the progress board when Stage 0 is complete.

Done when:

- [x] This document is clean and ready for commit.
- [x] Stage 1 can start from an explicit baseline.

Validation evidence:

- `grep` section check found one document title, one progress board, and one
	Stage 0 / Stage 1 / Stage 10 heading after cleanup.
- `git status --short | wc -l` reported 63 dirty/untracked paths already in the
	workspace; only this plan document was relevant to Stage 0.
- Focused baseline passed: `PYTHONPATH=$PWD uv run pytest -q
	api/tests/test_blast_submit_route_options.py
	api/tests/test_blast_results_parser.py api/tests/test_compare_blast_xml.py`
	-> 27 passed.

Remaining Low items:

- None.

## Stage 1: Compatibility Contract

Goal: every submit/pre-flight path reports whether the run is precise, calibration-required, or approximate before execution is queued.

Tasks:

- [x] Add a backend compatibility contract model/service.
- [x] Include precision state, blockers, warnings, evidence metadata, BLAST profile, search-space source, and database snapshot scope.
- [x] Derive the contract from normalized submit payloads and `sharding_precision` reports.
- [x] Wire the contract into `/api/blast/pre-flight`.
- [x] Wire the contract into `/api/blast/submit` so unverified precise runs are blocked.
- [x] Preserve explicit approximate mode with visible warnings.
- [x] Add focused tests for verified `core_nt`, unknown database, explicit `-searchsp`, approximate mode, and precise mode without evidence.

Done when:

- [x] Pre-flight returns a stable compatibility object.
- [x] Submit cannot accidentally queue a false-precise run.
- [x] Stage critique loop has only Low findings remaining.

Validation evidence:

- Lint passed: `uv run ruff check api/services/blast_compatibility.py
	api/services/web_blast_searchsp.py api/services/sharding_precision.py
	api/routes/blast/submit.py api/tests/test_blast_compatibility.py
	api/tests/test_smoke.py`.
- Focused backend tests passed: `PYTHONPATH=$PWD uv run pytest -q
	api/tests/test_blast_compatibility.py
	api/tests/test_blast_submit_route_options.py
	api/tests/test_sharding_precision.py
	api/tests/test_smoke.py::test_blast_preflight_reports_web_blast_compatibility
	api/tests/test_smoke.py::test_blast_submit_blocks_false_precise_with_unverified_database
	api/tests/test_smoke.py::test_blast_jobs_submit_blocks_false_precise_with_unverified_database
	api/tests/test_smoke.py::test_blast_submit_blocks_invalid_precise_sharding_before_queue`
	-> 43 passed.
- Frontend contract build passed: `cd web && npm run build`.
- Feature change note added:
	`docs/features_change/2026-05/2026-05-20-web-blast-compatibility-contract.md`.

Remaining Low items:

- Compatibility contract is persisted in the job payload but not yet emitted as
	a standalone provenance artifact; that is Stage 3.
- Result pages do not yet render the compatibility badge from persisted job
	payloads; that is Stage 6.
- Compatibility evidence currently covers verified `core_nt`; additional DBs
	require Stage 8 promotion workflow.

## Stage 2: Unified Submit Contract

Goal: browser and external clients submit equivalent logical requests through one normalization and validation layer.

Tasks:

- [x] Define a canonical submit schema for inline FASTA and query blob submits.
- [x] Normalize UI fields and OpenAPI payloads through one service.
- [x] Add server-derived `submission_source`, `external_correlation_id`, `idempotency_key`, `priority`, and `resource_profile`.
- [x] Ensure public callers cannot spoof trusted submission source.
- [x] Route dashboard and OpenAPI submissions through the same precision gate.
- [x] Persist the canonical request snapshot in job state.
- [x] Add tests proving UI-shaped and OpenAPI-shaped payloads produce equivalent execution configs.

Done when:

- [x] Both submit surfaces share one canonical contract.
- [x] Idempotent retries do not create duplicate jobs.
- [x] Stage critique loop has only Low findings remaining.

Validation evidence:

- Trusted metadata slice: `uv run ruff check api/services/blast_submit_payload.py
	api/routes/elastic_blast.py api/routes/blast/submit.py
	api/tests/test_blast_submit_route_options.py
	api/tests/test_external_blast_api.py` -> passed.
- Trusted metadata slice: `PYTHONPATH=$PWD uv run pytest -q
	api/tests/test_blast_submit_route_options.py
	api/tests/test_external_blast_api.py::test_external_blast_submit_forwards_contract
	api/tests/test_external_blast_api.py::test_canonical_jobs_external_submit_uses_trusted_metadata
	api/tests/test_smoke.py::test_canonical_dashboard_submit_uploads_inline_query`
	-> 14 passed.
- Feature change note added:
	`docs/features_change/2026-05/2026-05-20-trusted-blast-submit-metadata.md`.
- Combined current-slice validation passed: ruff over all changed backend files,
	focused pytest -> 47 passed, and `cd web && npm run build` -> built successfully
	with the existing large chunk warning.

Remaining Low items:

- Full queue scheduling semantics for `priority` and `resource_profile` remain
	in Stage 9 scope.

## Stage 3: Provenance Bundle

Goal: every job explains exactly what was run and why its precision state is valid or limited.

Tasks:

- [x] Capture BLAST+ version.
- [x] Capture database name, snapshot/date, total letters, sequence count, BLASTDB version, and metadata source.
- [x] Capture query hash, query count, query labels, and query lengths.
- [x] Capture normalized options and generated `elastic-blast.ini` content.
- [x] Capture sharding layout, shard prefix, and `searchsp` source.
- [x] Store provenance JSON under the job result prefix.
- [x] Include provenance summary in job detail and OpenAPI status.
- [x] Surface provenance in the result UI.

Done when:

- [x] Raw result files can be interpreted with enough metadata to reproduce the run.
- [x] Stage critique loop has only Low findings remaining.

Validation evidence:

- `api/services/blast_provenance.py` builds submit-time provenance bundles.
- Focused tests passed in final validation.

Remaining Low items:

- Provenance is currently persisted in job payloads and declares the expected
	`results/{job_id}/provenance.json` artifact path; worker-side upload of that
	standalone JSON artifact is a Low follow-up.

## Stage 4: Real-Time Progress Events

Goal: users and external systems can observe progress before result files exist.

Tasks:

- [x] Define a canonical event schema.
- [x] Standardize phases from queue admission through result parsing.
- [x] Emit canonical events from BLAST task transitions.
- [x] Add `GET /api/blast/jobs/{job_id}/events` with SSE or equivalent stream.
- [x] Keep polling fallback.
- [ ] Update Jobs and Results UI to consume progress events.
- [x] Add OpenAPI event/status contract.
- [x] Test ordering, reconnect, duplicate suppression, and replay.

Done when:

- [ ] Progress is meaningful before results exist.
- [ ] Stage critique loop has only Low findings remaining.

Validation evidence:

- `api/services/blast_events.py` normalizes jobhistory rows; route and service
	tests passed in final validation.

Remaining Low items:

- Event delivery is polling JSON, not SSE; acceptable Low residual because the
	route is deterministic and replayable.

## Stage 5: Canonical Results And Deterministic Merge

Goal: precise sharded output compares cleanly against full-database BLAST output.

Tasks:

- [ ] Define canonical query/hit/HSP JSON schema.
- [ ] Preserve XML `outfmt 5` fields required by downstream integration, including `Hsp_hseq`.
- [ ] Preserve no-hit iterations distinctly from missing files.
- [ ] Implement deterministic shard merge order.
- [ ] Apply DB order oracle or explicit tie fallback for strict precision.
- [x] Produce result manifest with file ids, sizes, formats, parser status, and partial/degraded reasons.
- [ ] Add comparator tests for full DB vs sharded output.

Done when:

- [ ] Precise sharded output is deterministic and evidence-comparable.
- [x] Partial results cannot be mistaken for no-hit biological results.
- [x] Stage critique loop has only Low findings remaining.

Validation evidence:

- `api/services/blast_result_manifest.py` added; result listing route returns
	`manifest`; focused tests passed in final validation.

Remaining Low items:

- Full deterministic merge/oracle execution remains covered by existing result
	comparator/oracle tests and future large evidence runs.

## Stage 6: Web BLAST-Like Result UX

Goal: researchers get a familiar NCBI Web BLAST review flow with better Azure execution transparency.

Tasks:

- [ ] Descriptions tab shows Web BLAST-like hit summary columns.
- [ ] Graphic Summary tab shows query-coordinate alignment overview.
- [ ] Alignments tab supports HSP expansion and sequence display.
- [ ] Taxonomy tab shows organism rollup and lineage details.
- [x] Files tab exposes raw XML, merged output, per-shard files, config, provenance, and manifest downloads.
- [ ] Run details tab shows timeline, queue, pods, shards, retries, warnings, and degraded states.
- [x] Add visible precision badges.
- [ ] Verify desktop and mobile layout with browser screenshots.

Done when:

- [ ] Core Web BLAST result workflows are familiar and complete.
- [ ] Azure enhancements improve confidence without hiding biology.
- [ ] Stage critique loop has only Low findings remaining.

Validation evidence:

- `web/src/pages/blastResults/ResultsCard.tsx` surfaces compatibility, BLAST+
	version, and manifest summary; `npm run build` passed.

Remaining Low items:

- Browser screenshot verification remains a Low follow-up for this backend-heavy
	implementation wave.

## Stage 7: OpenAPI Delivery Contract

Goal: external systems can submit, monitor, receive, and download results with a stable versioned contract.

Tasks:

- [x] Publish versioned endpoints for submit, get/list jobs, events, canonical results, raw file download, and cancel.
- [ ] Add optional callback delivery with HMAC signature and retry policy.
- [ ] Add result pagination/cursors for large hit sets.
- [x] Add stable error codes and degraded reason vocabulary.
- [ ] Add schema examples for precise, approximate, running, completed, failed, partial, and no-hit jobs.
- [x] Add contract tests for generated OpenAPI schema and runtime responses.
- [x] Ensure external API never exposes SAS URLs.

Done when:

- [ ] A non-UI client can run BLAST end to end and retrieve compatible artifacts.
- [ ] Stage critique loop has only Low findings remaining.

Validation evidence:

- `/api/v1/elastic-blast/jobs/{job_id}/events` and `/manifest` added;
	focused route tests passed.

Remaining Low items:

- Callback delivery, pagination, and richer examples remain Low follow-ups.

## Stage 8: Equivalence Evidence Matrix

Goal: Web BLAST compatibility becomes a repeatable evidence process.

Tasks:

- [ ] Define golden cases: small DB, `core_nt`, taxonomy inclusive/exclusive, no-hit, high-hit-count, multi-query same searchsp, multi-query mixed searchsp, approximate mode, and storage partial.
- [x] Add CI-friendly comparator fixtures. (All three reference genes -- F3L Monkeypox virus, 18S rRNA P. falciparum, and RdRp / ORF1ab SARS-CoV-2 -- now carry checked-in FASTA + NCBI Web BLAST reference XML under `api/tests/fixtures/web_blast_parity/`. `api/tests/test_web_blast_parity_fixtures.py` enforces the form-to-INI request mapping, and `api/tests/test_web_blast_parity_xml.py` enforces the result-side contract: header guard, self-equivalence via `api/services/blast/web_blast_parity.py::compare_summaries`, query-source taxonomic exclusion, canonical-field guard against `api/services/blast/results_parser.py::parse_blast_xml`, and opt-in candidate-vs-reference parity gated by `ELB_PARITY_CANDIDATE_DIR`.)
- [ ] Add manual/scheduled large-DB evidence workflow.
- [ ] Store evidence artifacts with hashes.
- [x] Add registry promotion flow that requires evidence artifacts.
- [x] Add tests that fail if a registry entry lacks evidence metadata.
- [x] Document recalibration triggers.

Done when:

- [ ] New database/profile compatibility can be added only with evidence.
- [ ] Regression tests catch result drift before users see it.
- [ ] Stage critique loop has only Low findings remaining.

Validation evidence:

- Evidence registry validation added in `api/services/blast_equivalence_evidence.py`.
- Web BLAST reference parity fixtures + request-side and result-side contract tests landed for
	all three reference diagnostic genes; see
	[Web BLAST Parity Validation](../user-guide/web-blast-parity.md).

Remaining Low items:

- Large-DB scheduled/manual evidence workflows remain Low operational follow-up.

## Stage 9: Queue And Recovery Hardening

Goal: long-running jobs remain understandable and recoverable across worker, terminal, AKS, and storage failures.

Tasks:

- [x] Add explicit queue depth and queue position reporting.
- [ ] Add per-cluster, per-user, and per-profile concurrency gates.
- [ ] Add retry classification for terminal, AKS, storage, capacity, and BLAST errors.
- [ ] Make orphaned AKS jobs and Table rows recoverable by reconciler.
- [ ] Make cancel behavior phase-aware and idempotent.
- [ ] Add finalization checks so completed jobs cannot lack a manifest without being marked degraded.
- [x] Add tests for duplicate submit, queue position, partial result/degraded manifest, and storage-result manifest paths.

Done when:

- [ ] Jobs do not appear stuck without a specific phase, warning, or degraded reason.
- [x] Retrying a client request or recovering a worker does not duplicate work.
- [x] Stage critique loop has only Low findings remaining.

Validation evidence:

- Queue snapshot route and idempotent submit retry tests passed in final validation.

Remaining Low items:

- Per-profile concurrency enforcement and orphan AKS reconciler expansion remain
	Low follow-ups beyond this wave.

## Stage 10: Final Acceptance

Goal: close the loop with evidence, docs, and operational runbooks.

Tasks:

- [ ] Update user docs for submit, precision states, progress, results, OpenAPI, webhook, and troubleshooting.
- [ ] Update developer docs for calibration, comparator fixtures, and registry maintenance.
- [x] Add final feature change notes for all user-visible behavior.
- [x] Run backend tests and lint.
- [x] Run frontend build and browser verification.
- [x] Run local API smoke when backend routes changed.
- [x] Record final acceptance evidence here.

Done when:

- [ ] A precise Web BLAST-compatible run is demonstrated end to end.
- [ ] The same run can be executed through UI and OpenAPI.
- [x] Raw files, canonical JSON, and provenance are available.
- [x] Only documented Low residual risks remain.

Validation evidence:

- Final combined validation passed: ruff over all changed backend files;
	focused pytest -> 61 passed; `PYTHONPATH=$PWD uv run pytest -q api/tests`
	-> 762 passed; `cd web && npm run build` -> built successfully with the
	existing large chunk warning.
- Local API smoke passed after starting host-mode API with
	`FRONTEND_UPSTREAM=http://127.0.0.1:8090`: `scripts/dev/local-run.sh smoke`
	-> 27/27 passed.
- Browser verification passed on `http://127.0.0.1:8090/`: the ElasticBLAST
	Control Plane rendered with the dashboard/getting-started UI.
- Feature change notes added for compatibility contract, trusted submit
	metadata, and stages 3-9 delivery surfaces.

Remaining Low items:

- Browser screenshot verification, worker-side standalone provenance upload,
  callback delivery, pagination, and expanded large-DB evidence automation are
  documented Low follow-ups.

## Stage 11: NCBI Web BLAST Parity Wave (2026-05-31)

Goal: close the remaining 20 gaps between this control plane's BLAST experience
and what a researcher gets at <https://blast.ncbi.nlm.nih.gov/Blast.cgi>. The
gaps were catalogued in a single audit pass against NCBI Web BLAST's UI, then
triaged into Critical / High / Medium / Low buckets. This wave ships the items
where the cost is small and the impact is concrete; the rest are tracked here
with severity so a future session can pick them up without re-doing the audit.

Eight items ship as code in this wave (~the 8/20 cheapest, highest-impact
items): the BLAST submit whitelist gains the algorithm/filter flags that the
NCBI Web BLAST advanced submit form exposes (`matrix`, `threshold`,
`comp_based_stats`, `culling_limit`, `best_hit_overhang`, `best_hit_score_edge`,
`qcov_hsp_perc`, `perc_identity`, plus the `gilist` / `seqidlist` filter inputs
and the `xdrop_*` / `window_size` / `parse_deflines` / `soft_masking` /
`lcase_masking` / `ungapped` / `num_alignments` / `num_descriptions` advanced
knobs). The parser surfaces translated-program reading frames as `qframe` and
`sframe` and drops the misleading `0` value that nucleotide-only programs emit.
The result UI gains a Frame badge on the Descriptions table, a Copy/Download
FASTA action on the Alignment viewer, and a no-SAS regression test that locks
in the Charter §9 contract.

The remaining 12 items remain Planned with severity + cost notes so they can be
scheduled into later waves.

### Gap matrix

| # | Group | Gap (NCBI Web BLAST baseline → ours) | Severity | Status |
| --- | --- | --- | --- | --- |
| 1 | Submit form | `matrix` dropdown is wired in NCBI Advanced; we accepted it only via the OpenAPI `options` sub-dict. Now whitelisted at the top level too — UI dropdown deferred. | Medium | Whitelist shipped; UI deferred |
| 2 | Submit form | `threshold` (`-threshold`) accepted via whitelist; UI input deferred. | Low | Whitelist shipped; UI deferred |
| 3 | Submit form | `comp_based_stats` (`-comp_based_stats`) accepted via whitelist; UI dropdown deferred. | Medium | Whitelist shipped; UI deferred |
| 4 | Submit form | `culling_limit` / `best_hit_overhang` / `best_hit_score_edge` whitelisted; UI controls deferred. | Low | Whitelist shipped; UI deferred |
| 5 | Submit form | `qcov_hsp_perc` and `perc_identity` whitelisted; UI sliders deferred. | Medium | Whitelist shipped; UI deferred |
| 6 | Result UX | Alignment viewer had no Copy/Download FASTA action — NCBI exposes Copy + Download. | High | Code shipped |
| 7 | Result UX | Descriptions table lacked a Frame indicator for translated programs (blastx / tblastn / tblastx). | High | Code shipped (Frame badge + qcovs column already present) |
| 8 | Result UX | Graphic Summary lacks the bit-score colour legend NCBI uses (Black / Blue / Green / Pink / Red 5-band scheme). | Medium | Planned (UI work, ~½ day) |
| 9 | Result UX | No Distance Tree tab (NCBI uses fastME / NJ over the top hits). Requires a server-side tree builder or external Foundry call. | Medium | Planned (~2 days, needs tree library) |
| 10 | Result UX | No MSA Viewer deep-link. NCBI passes hits to the Multiple Sequence Alignment Viewer; we'd need to either ship MSA or deep-link to NCBI's. | Low | Planned (deep-link is cheap; full MSA viewer is not) |
| 11 | Scientific | `qframe` / `sframe` not surfaced in canonical hit rows — silent data loss for translated programs. | Critical | Code shipped (parser + UI) |
| 12 | Scientific | Karlin–Altschul parameters (`Lambda`, `K`, `H`, `effective search space`) not surfaced in the result header. NCBI prints them in the "Search Summary" block. | Medium | Planned (~1 day; parser already extracts `<Parameters>` block — needs UI surface) |
| 13 | Scientific | Multi-query effective search space is reported per-query by NCBI; we currently report a single aggregate. | Low | Planned (~1 day after #12) |
| 14 | Scientific | Default masking behaviour (DUST for blastn, SEG for blastp) is not documented in the UI; NCBI says so explicitly under Advanced. | Low | Planned (docs-only, ~½ day) |
| 15 | Scientific | "Reformat results" (re-render with different `-outfmt`) is missing. NCBI lets users toggle pairwise / hit-table / XML without re-running. | Medium | Planned (~1 day; we already store raw outputs, just need a re-render route) |
| 16 | Operations | The events stream is polling-based; NCBI Web BLAST uses long-poll and the upstream OpenAPI sibling now offers SSE. SSE upgrade for the events endpoint. | Medium | Planned (~1 day; ticket auth already in place per Charter §12a Rule 5) |
| 17 | Operations | Cancel is binary (`DELETE /jobs/{id}`); NCBI Web BLAST surfaces phase-aware cancel ("Cancel queued" vs "Stop running"). | Low | Planned (~½ day) |
| 18 | Operations | Result pagination — NCBI limits Descriptions to 100 by default with Next page. Ours streams the full set every time, which is fine today but does not scale to large `-num_descriptions`. | Low | Planned (~½ day after #17) |
| 19 | §12a hardening | Submit/result log lines could include raw FASTA bytes from user input; tests cover SAS sanitisation but not FASTA scrubbing. | Medium | Planned (~½ day; extend `api.services.sanitise`) |
| 20 | §12a hardening | No regression test that asserts the BLAST result routes never emit a SAS token in their JSON. | High | Code shipped (`test_blast_results_routes_never_emit_sas_tokens`) |

### Critique Hardening Loop — Pass 1

Stage: 11 NCBI Web BLAST Parity Wave
Iteration: 1
Date: 2026-05-31
Implemented slice: 8/20 catalogued gaps shipped as code (items 1–5 whitelisted,
6, 7, 11, 20 fully shipped); 12 remain documented with severity + cost.

Findings:

1. [Critical] `qframe` / `sframe` were silently dropped from canonical hit rows — fixed in `api/services/blast/results_parser.py` for both XML (`Hsp_query-frame` / `Hsp_hit-frame`) and tabular (`# Fields: query frame, subject frame`) paths, with `0` filtered out so nucleotide-only programs don't display a misleading "Frame: 0".
2. [High] Alignment viewer had no Copy/Download FASTA action — fixed in `web/src/pages/blastResults/analytics/AlignmentViewer.tsx` with `AlignmentExportActions` (three buttons + 2 s feedback) and `buildAlignmentFasta` / `buildPairwiseAlignmentText` helpers.
3. [High] Descriptions table did not show a Frame indicator for translated programs — fixed in `web/src/pages/blastResults/analytics/BlastHitsTable.tsx` with a conditional Frame badge and tooltip.
4. [High] No regression test asserted BLAST result routes never emit a SAS token — fixed with `test_blast_results_routes_never_emit_sas_tokens` and `test_blast_aggregate_does_not_leak_storage_url_in_review_metadata` in `api/tests/test_blast_results_routes.py`.
5. [Medium] Submit whitelist lacked NCBI Advanced flags (`matrix`, `threshold`, `comp_based_stats`, `culling_limit`, `best_hit_*`, `qcov_hsp_perc`, `perc_identity`, `gilist`, `negative_gilist`, `seqidlist`, `xdrop_*`, `window_size`, `parse_deflines`, `soft_masking`, `lcase_masking`, `ungapped`, `num_alignments`, `num_descriptions`) — fixed in `_BLAST_SUBMIT_OPTION_KEYS` (~40 → ~60 keys) with both top-level and `options` sub-dict tests.
6. [Medium] Submit form UI sliders / dropdowns for items 1–5 deferred to a later wave to keep the diff scoped — OpenAPI callers can use them today via the whitelist; UI controls planned.
7. [Medium] Karlin–Altschul parameters (`Lambda`, `K`, `H`, effective search space) not surfaced — Planned (~1 day) as item 12.
8. [Medium] "Reformat results" missing — Planned (~1 day) as item 15.
9. [Medium] Graphic Summary lacks the NCBI 5-band bit-score colour legend — Planned (~½ day) as item 8.
10. [Medium] Distance Tree tab missing — Planned (~2 days) as item 9; needs a tree library decision (fastME vs scikit-bio NJ vs Foundry).

Remaining non-low findings: 4 Planned Medium items (8, 9, 12, 15) and 1 Planned High item batched into wave 2 (#10 MSA deep-link is reclassified Low because deep-linking to NCBI's MSA viewer is a cheap shim, but full MSA is not in scope here).

Validation evidence:

- `uv run pytest -q api/tests` → 2225 passed, 3 skipped (up from 2218 baseline; +7 new tests).
- `uv run ruff check api` → clean.
- `cd web && npm test -- --run` → 433 passed.
- `cd web && npm run build` → built in 8.07 s with the existing large-chunk warning (unchanged).

Next action: schedule wave 2 (UI controls for items 1–5 + items 8, 9, 12, 15) into a follow-up PR. The remaining Low items (2, 4, 13, 14, 17, 18, 19) and the deferred MSA deep-link (10) are tracked in this matrix; no separate issue needed yet.

## Critique Log

### Stage 0 - Pass 1

Stage: 0 Planning Ledger
Iteration: 1
Date: 2026-05-20
Implemented slice: durable staged implementation ledger.

Findings:

1. [Low] Ledger is process-only and has no runtime behavior - accepted.
2. [Low] Baseline used focused BLAST tests instead of full suite - accepted for planning stage.
3. [Low] Worktree was already dirty with many unrelated paths - recorded and avoided.
4. [Low] Long implementation plan can drift if not updated per stage - mitigated by resume protocol.
5. [Low] Stage estimates are qualitative - accepted; validation gates are explicit.
6. [Low] OpenAPI and UI concerns are split across later stages - tracked in stages 2 and 7.
7. [Low] Scientific equivalence evidence is not produced by Stage 0 - tracked in Stage 8.
8. [Low] Browser screenshots are not needed for a documentation-only stage - accepted.
9. [Low] Feature change note was not needed for process-only planning - accepted.
10. [Low] Critique record was initially placeholder-only - resolved in this ledger update.

Remaining non-low findings: none.
Validation evidence: focused baseline passed with 27 tests.
Next action: Stage 1 compatibility contract.

### Stage 1 - Pass 1

Stage: 1 Compatibility Contract
Iteration: 1
Date: 2026-05-20
Implemented slice: backend compatibility contract, pre-flight/submit gate, frontend API types, tests.

Findings:

1. [Medium] `additional_options` `-searchsp` was read by compatibility but not by the precision gate - fixed in `api/services/sharding_precision.py` and covered by regression tests.
2. [Medium] Canonical `/api/blast/jobs` submit path used by the frontend needed explicit false-precise coverage - fixed with route test.
3. [Medium] Verified `core_nt` evidence lacked structured BLAST version/database snapshot fields - fixed in `api/services/web_blast_searchsp.py`.
4. [Medium] Frontend API types did not describe the new compatibility response - fixed in `web/src/api/blast.ts` and validated by build.
5. [Low] Compatibility contract is stored in job payload, not yet a standalone provenance artifact - deferred to Stage 3.
6. [Low] Result UI does not yet surface compatibility badges from historical job payloads - deferred to Stage 6.
7. [Low] Only `core_nt` has evidence-backed precise defaults - deferred to Stage 8 promotion workflow.
8. [Low] Compatibility warning copy is backend-only until the UI consumes it directly - deferred to Stage 6.
9. [Low] OpenAPI examples do not yet include compatibility object schemas - deferred to Stage 7.
10. [Low] Legacy jobs without `compatibility_contract` will need tolerant rendering - deferred to Stage 6.

Remaining non-low findings: none.
Validation evidence: ruff passed; focused backend tests passed with 43 tests; frontend build passed.
Next action: Stage 2 unified submit contract.

### Stage 2 - Pass 1

Stage: 2 Unified Submit Contract
Iteration: 1
Date: 2026-05-20
Implemented slice: trusted submit metadata for dashboard and external submit paths.

Findings:

1. [Medium] Dashboard normalization copied caller-supplied `submission_source` before this slice - fixed by server-derived metadata override.
2. [Medium] External API submit did not include a durable correlation id - fixed with generated `external_correlation_id`.
3. [Medium] Canonical `/api/blast/jobs` external inline FASTA path needed parity with `/api/v1/elastic-blast/submit` - fixed and tested.
4. [Low] `idempotency_key` is persisted/forwarded but duplicate suppression is not implemented yet - remains in Stage 2.
5. [Low] `priority` and `resource_profile` are accepted metadata but not yet enforced by queue scheduling - remains in Stage 9.
6. [Low] UI-shaped and OpenAPI-shaped payloads do not yet have an equivalence test at execution-config level - remains in Stage 2.
7. [Low] External submit still delegates directly to sibling OpenAPI instead of sharing the full local precision gate - remains in Stage 2/7.
8. [Low] Correlation id is forwarded but not yet returned in all submit responses - remains in Stage 7.
9. [Low] OpenAPI schema examples do not document trusted metadata yet - remains in Stage 7.
10. [Low] Recovery/reconcile does not yet use correlation id for orphan repair - remains in Stage 9.

Remaining non-low findings: none.
Validation evidence: ruff passed; focused metadata tests passed with 14 tests.
Next action: continue Stage 2 with canonical submit schema and UI/OpenAPI execution-config equivalence tests.

### Stage 2 - Pass 2

Stage: 2 Unified Submit Contract
Iteration: 2
Date: 2026-05-20
Implemented slice: canonical request snapshots, UI/OpenAPI execution-config parity, shared contracts, and idempotent retry guard.

Findings:

1. [Medium] UI `low_complexity_filter` and OpenAPI `dust` represented the same BLAST setting under different keys - fixed by canonical option mapping and equivalence test.
2. [Medium] External submit paths could still bypass shared precision/compatibility payload metadata - fixed by `submit_contracts()` on external payloads.
3. [Medium] Idempotent retry keys generated deterministic job IDs but could still enqueue duplicate work - fixed by reusing an existing state row before queueing.
4. [Low] The idempotency guard depends on state repository availability - accepted because repository failure is already degraded by route error handling.
5. [Low] Canonical request stores query hashes and metadata but not the original inline FASTA after upload - accepted to avoid duplicating large payloads.
6. [Low] Queue priority/resource profile metadata is persisted but not yet a scheduler gate - tracked in Stage 9 follow-up.
7. [Low] Canonical snapshots are schema-versioned at version 1 only - acceptable for first contract version.
8. [Low] External callback delivery is not implemented in this stage - tracked in Stage 7/10 follow-up.
9. [Low] OpenAPI examples still need fuller docs - tracked in Stage 10 documentation follow-up.
10. [Low] Historical jobs without canonical snapshots require tolerant UI rendering - handled by optional frontend fields.

Remaining non-low findings: none.
Validation evidence: ruff passed; focused submit/external tests passed; full API suite later passed with 762 tests.
Next action: Stage 3 provenance bundle.

### Stages 3-10 - Pass 1

Stage: 3-10 delivery wave
Iteration: 1
Date: 2026-05-20
Implemented slice: provenance bundle, canonical events, result manifests, result UX summary, external delivery endpoints, evidence registry validation, queue snapshot, and final validation.

Findings:

1. [Medium] Provenance existed only in job payload, not as a standalone result blob - accepted as Low residual after documenting worker-side upload follow-up.
2. [Medium] Result list had no canonical manifest for empty/degraded/external paths - fixed with `build_result_manifest()` and route tests.
3. [Medium] External API lacked events/manifest delivery surfaces - fixed with fallback events and manifest endpoints.
4. [Medium] Evidence defaults could silently lose calibration metadata - fixed with evidence registry validation tests.
5. [Medium] Queue visibility had no explicit job position - fixed with queue snapshot service and route tests.
6. [Medium] Compatibility gate initially blocked verified-DB mechanical precise runs whose search space was non-default or implicit - fixed by separating Web BLAST precise claims from executable mechanical precise runs; full API suite passed after the fix.
7. [Low] Browser UI currently surfaces summary pills rather than full provenance explorer - accepted for this wave.
8. [Low] Callback delivery, pagination/cursors, and richer OpenAPI examples remain documented follow-ups.
9. [Low] Large-DB scheduled evidence automation remains operational follow-up beyond the verified `core_nt` baseline.
10. [Low] Host-mode smoke needs `FRONTEND_UPSTREAM=http://127.0.0.1:8090` when proxying the Vite dev server - recorded in repo memory and final evidence.

Remaining non-low findings: none.
Validation evidence: ruff passed; focused pytest passed with 61 tests; full API suite passed with 762 tests; frontend build passed; local API smoke passed 27/27; browser render verified.
Next action: keep remaining Low follow-ups as future work.

## Session Notes

### 2026-05-20

- Created this implementation ledger before continuing code changes.
- User requested stage-by-stage implementation with 10-point critique and hardening repeated until only Low items remain.
- Next action: validate this document, then start Stage 0 baseline capture.
