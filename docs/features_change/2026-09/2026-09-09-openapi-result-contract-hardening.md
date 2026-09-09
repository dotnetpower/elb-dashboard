---
title: OpenAPI result readiness and selection contracts
description: Gate partitioned completion on canonical artifacts, align active database metadata, and expose explicit native or diversity-aware result selection.
tags:
  - blast
  - architecture
  - user-guide
---

# OpenAPI result readiness and selection contracts

## Motivation

Two retained production jobs exposed several cross-surface contract gaps. Their public status became
`completed` while only ten shard files were visible; `merged_results.out.gz` appeared 6 minutes 23
seconds and 6 minutes 40 seconds later. An artifact snapshot built during that interval remained
shard-only after the canonical merge. The database detail endpoint also returned an older metadata
snapshot than the active generation used by precise execution. Finally, direct `/v1/jobs` tabular
requests received only the raw `score` column while Service Bus requests received the complete
result-analysis field set.

Precise DB-order selection itself behaved as implemented: it selected 5,000 distinct subjects with
the native BLAST comparator and intentionally made no diversity reservation. The prior near-miss
announcement did not make that exact-versus-diverse boundary sufficiently explicit for API callers.

## User-facing change

- A partitioned job does not become `completed` until `merged_results.out.gz` is discoverable.
- Successful status payloads expose `results_ready`, `results_ready_at`, and `merged_at` for
  partitioned jobs. Clients wait for `results_ready=true` instead of using a fixed post-completion
  retry window.
- The merge finalizer has a 30-minute active deadline. Terminal finalizer failure becomes
  `failed` / `finalizer_failed` instead of an unbounded `finalizing` state.
- Early shard-only result artifacts are invalidated and rebuilt after canonical result readiness.
- Stale artifact invalidation is generation-guarded with an Azure Table ETag. A reader holding an
  older shard-only payload cannot overwrite a concurrently rebuilt `ready` artifact, and the
  terminal-artifact reconciler does not enqueue another attempt unless its bounded attempt count is
  durably stored.
- `GET /v1/databases/core_nt` and its always-on dashboard mirror overlay the active generation's
  snapshot, sequence count, and letter count used by execution.
- Direct `/v1/jobs` and Service Bus tabular submissions append the same missing fields, in order:
  `staxids`, `sscinames`, `stitle`, `qcovs`, `score`. Caller fields retain their original order and
  merged outfmt 7 output retains the authoritative `# Fields:` header.
- `blast_options.db_effective_search_space` is a typed direct-submit field. Normal `core_nt`
  callers omit it and raw `-searchsp` / `-dbsize`; the active generation owns the value. Combining
  the typed field with either raw option is rejected as ambiguous.
- `blast_options.web_blast_statistical_context` is available on `/v1/jobs` for measured
  single-query filtered statistics. It is optional and is not synthesized from unfiltered database
  counts.
- `blast_options.result_selection_policy` is `native_top_n` by default. The opt-in
  `diversity_aware` mode retains proportional lower-score subject representation when one tied
  score class overflows the result window; it deliberately does not claim native full-database
  hit-list equality.

## API and runtime summary

- The OpenAPI build-context patch gates partitioned completion and result listing on the canonical
  merged artifact, and carries additive readiness fields on both status surfaces.
- Durable `db_partitions` and `result_selection_policy` provenance no longer depend on exact-oracle
  presence. Diversity-aware execution skips the multi-gigabyte DB-order oracle download and sets
  the merger's bounded proportional policy explicitly.
- The [ElasticBLAST](https://blast.ncbi.nlm.nih.gov/doc/elastic-blast/) finalizer Job receives an
  `activeDeadlineSeconds: 1800` backstop. The OpenAPI Kubernetes summary recognizes a terminal
  finalizer failure.
- Dashboard result artifact finalization performs bounded readiness retries and marks exhaustion
  explicitly without changing the scientific job's terminal outcome.
- Existing request fields and response fields remain compatible; every new field is additive and
  defaults to prior native top-N behavior.
- Artifact readiness failures remain separate from the scientific job outcome. They are observable
  through bounded `artifact_finalizer` state and history without rewriting a completed run as
  failed.
- No authentication, RBAC, network, Storage-public-access, or infrastructure topology contract
  changed.

## Validation

- Focused backend contract sweep: 686 passed, covering canonical completion, readiness fields,
  active metadata, typed search space, selection policy, generated source, finalizer templates,
  Service Bus parity, artifact ETag races, and bounded reconciliation.
- Full backend suite: 5,792 passed, 5 environment-dependent evidence checks skipped. Full frontend
  suite: 1,023 passed across 116 files. API Reference spec tests: 5 passed.
- The patcher applied twice, compiled, and passed all 113 `docker-openapi` tests against a temporary
  clone of sibling source `352a1f4c` using its declared runtime and test dependencies.
- `ruff check`, the production mypy debt ratchet, the 242-operation OpenAPI contract check,
  generated TypeScript drift check, ESLint, the Vite production build, docs frontmatter guard, and
  strict MkDocs build passed.
- Local host-mode smoke passed 27/27 endpoints. Playwright rendered `/docs` with no console or page
  errors at 1440 x 1000 and 390 x 844; both viewports had no horizontal overflow or clipped
  interactive text. Desktop and mobile screenshots were captured in the validation session.
- The production investigation was read-only. No job was submitted, no Azure resource was started,
  no configuration was changed, and no deployment was performed. The OpenAPI runtime source change
  follows the normal immutable image build-and-pin release flow rather than overwriting tag `4.52`.
