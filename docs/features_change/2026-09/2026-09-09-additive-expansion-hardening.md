---
title: Additive BLAST expansion hardening
description: Harden six additive BLAST features through repeated contract, queue, security, scientific, UI, and supply-chain reviews.
tags: [blast, security, contributor]
---

# Additive BLAST expansion hardening

## Scope and invariant

This review covers the reproducibility package, split-job details, completed-result comparison, runtime/cost estimate, OpenAPI compatibility tooling, and restored saved templates. The source behavior baseline is commit `476a12014820c75f8d276475bbdd64f1e866345b`.

Existing [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview) request-queue and completion-topic behavior was protected throughout. No queue/topic name, wire payload, admission decision, atomic claim, single-flight drain, settlement, durable outbox, retry, completion subscription, or shared configuration code changed.

## Review method

Two complete passes used twelve independent review rounds. A finding was accepted as Medium or higher only when the changed feature had a reproducible wrong result, hidden failure, security boundary bypass, crash, or blocked workflow. Missing tests alone, preferences, unchanged legacy paths, and general technical debt were not promoted.

| Round | Review lens | Outcome after fixes |
| --- | --- | --- |
| 1 | Backward compatibility and OpenAPI | Baseline 238 operations to current 242 operations; zero breaking changes. Tightened constraints, union-variant removal, and security changes are now detected. |
| 2 | Consumer, generated type, and fixture parity | Config export now includes accession and taxonomy-column mode; template persistence still excludes per-run query fields. Generated declarations are current. |
| 3 | Service Bus request queue and completion topic | Nine protected source hashes match the baseline; 354 backend and 43 frontend contract tests pass. |
| 4 | State, idempotency, and concurrency | New runtime routes are read-only. No Celery task, state transition, retry loop, queue write, or result mutation was added. |
| 5 | Authentication, personas, ownership, and privacy | Every new route uses `require_caller`; both comparison jobs are checked independently; templates remain owner-partitioned. |
| 6 | Resource bounds and denial of service | Comparison reuses file/byte/HSP caps, shard responses probe 1,001 and return at most 1,000 with `truncated=true`, and templates accept only known finite scalar options. |
| 7 | Scientific and data correctness | Equivalent database paths normalize consistently, and HSP-count-only changes are detected even when e-values are unavailable. |
| 8 | Partial failure and observability | Template Storage failures return 503 instead of empty/404; reproducibility exports recursively remove query/secret/replay material and Storage credentials. |
| 9 | Frontend state, races, and cache behavior | Template application preserves the current query/run identity; stale runtime estimates are hidden during the debounce window. |
| 10 | Accessibility, responsive layout, and interaction | Load errors are announced, save focus is restored, deletion is confirmed, and 1,440 px plus 390 px browser checks show no page-level overflow from new controls. |
| 11 | CI/tooling portability and fail-closed behavior | OpenAPI generation uses only the pinned local executable and fails when dependencies are absent; baseline/type drift checks pass. |
| 12 | Supply chain, documentation, and operator safety | `npm audit` reports zero vulnerabilities; Ruff, ESLint, strict docs build, mypy debt ratchet, and production build pass. No deployment or shared Azure mutation was performed. |

A browser-only follow-up found one additional Medium workflow defect: opening a completed job directly on `?tab=run` was interpreted as a live completion transition and redirected to Descriptions. Phase observations now start only after the same job loads, so direct Run details links survive while a genuinely running job still switches to Descriptions when it completes.

The second twelve-round pass found no remaining Medium, High, or Critical defect in the changed feature surface.

## Fixed findings

- Prevented raw query, accession, blob/file references, ranges, job title, and execution identity from entering saved templates through direct API calls or legacy rows.
- Distinguished template Storage outages from empty lists and missing records.
- Normalized database paths before comparison and detected HSP-count-only changes.
- Defined comparison direction as the current job relative to the selected
	baseline and rejected missing result artifacts rather than claiming empty
	result sets are identical.
- Bounded comparison response materialization and long identifiers, and used
	current-result metadata for changed hits.
- Added explicit shard-response truncation and a UI warning.
- Bounded and sanitized corrupt shard projection fields.
- Recursively redacted secret/query keys and credential-bearing Storage URLs in portable packages.
- Suppressed stale runtime estimates while changed input waits for debounce.
- Preserved completed-job Run details deep links.
- Reset comparison mutation state when the route changes jobs, kept truncated
	active shard jobs polling, and made the comparison form fit a 320 px viewport.
- Removed query/subject CLI flags hidden in template `additional_options`,
	validated reusable values by field type/range, bounded legacy listings, and
	enforced duplicate names on rename.
- Corrected `allOf` compatibility direction and added map-value constraint
	detection to the OpenAPI gate.
- Expanded OpenAPI compatibility detection and removed implicit network package installation.

## Residual Low risks

- The per-owner 50-template count check is read-then-create, so two simultaneous creates could produce 51 rows. Ownership, row size, and total practical impact remain bounded.
- Comparison candidate discovery shows at most 100 recent jobs; the job-id input remains available for older jobs.
- Runtime estimates remain observational and can have low confidence; the UI labels sample count/confidence and never uses the value for admission or submission.
- The production bundle retains the existing Rollup warning for chunks above 500 kB.
- Build-time OpenAPI structure checks cannot replace semantic human review of implementation behavior.

## Validation evidence

- Full backend: `5777 passed, 5 skipped` (external parity evidence unavailable for the five conditional tests).
- Full frontend: `1023 passed`.
- Service Bus backend: `354 passed`; Message Flow frontend: `43 passed`.
- API smoke: `27/27 passed` against `http://127.0.0.1:8085`.
- OpenAPI bootstrap comparison: `238 -> 242` operations, zero breaking changes.
- OpenAPI current baseline: 242 operations; generated TypeScript declarations current.
- Mypy debt ratchet: unchanged at 502 errors across 97 production files.
- `npm audit`: zero vulnerabilities.
- Browser checks: desktop 1,440 x 1,000 and mobile 390 x 844 for templates, shard details, comparison, and reproducibility download response/toast.
- No deployment, live BLAST submit, Service Bus send/receive/peek-lock/settlement, purge, completion consumption, or shared settings mutation was performed.
