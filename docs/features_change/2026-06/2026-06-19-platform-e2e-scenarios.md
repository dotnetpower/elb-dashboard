---
title: Integrated platform E2E scenario suite (cost-gated live tier)
description: Add a scenario-organised, integrated E2E pass over cluster stop/start + auto-stop, Service Bus queue + auto-start, direct API BLAST, queue management + parallelism, and permissions — each runnable standalone, with live Azure side effects behind explicit cost flags. Also fix a brittle MessageFlow card assertion.
tags:
  - contributor
  - blast
---

# Integrated platform E2E scenario suite

## Motivation

The maintainer asked for an expanded, integrated E2E pass that exercises the
whole control plane — cluster stop/start, idle auto-stop, Service Bus
queue-triggered auto-start, direct API BLAST submit → status → results, the same
through the Service Bus queue, permissions, max-parallelism + queue-wait, and
queue management — organised so each scenario also runs standalone, then run with
log / App Insights error verification.

## User-facing change

Contributor-facing only (no product behaviour change):

- New `scripts/e2e/scenarios/platform-flows.api.spec.ts` — an integrated,
  scenario-tagged E2E spec covering `scenario:aks-lifecycle`,
  `scenario:service-bus`, `scenario:api-blast`, `scenario:queue-parallel`, and
  `scenario:permissions`. It runs in the existing `api-smoke` Playwright project
  (so it joins `e2e:all-safe`) and each scenario is independently runnable.
- New npm scripts: `e2e:platform` (integrated) plus `e2e:platform-aks`,
  `e2e:platform-sb`, `e2e:platform-blast`, `e2e:platform-queue`,
  `e2e:platform-perms` (standalone, via Playwright `--grep`).
- Cost model: the default run is **cost 0** (read-only contract checks against
  the real local API; structured `503` degrades like `openapi_not_configured`
  are accepted). Every real Azure side effect is behind an explicit flag —
  `E2E_ALLOW_AKS_POWER`, `E2E_ALLOW_AKS_AUTOSTOP_MUTATE`, `E2E_ALLOW_SB_SEND`,
  `E2E_ALLOW_BLAST_SUBMIT` — so a normal run never spends money or mutates a live
  cluster. Persona/permission coverage stays in `api/tests/test_persona_matrix.py`.
- Fixed a brittle assertion in `message-flow-events.ui.spec.ts`: the card renders
  both a static `active jobs` eyebrow label and a `<N> active jobs` count, so the
  bare `/active jobs/` matcher tripped Playwright strict mode. Narrowed it to
  `/\d+ active jobs/`. The MessageFlow card itself is unchanged and correct.

## Validation evidence

- `npm run e2e:list` — the new spec compiles; all 13 scenario tests are
  discovered, grouped by `scenario:*` tag.
- `scripts/dev/e2e-ui.sh bypass --headless --fullstack -- npm --prefix web run e2e:all-safe`
  — **36 passed, 6 skipped** (the 6 skipped are the live-gated mutations), EXIT 0.
- Backend scenario suites: `pytest -q` over `test_persona_matrix`,
  `test_auto_stop_evaluator`, `test_auto_stop_sb_signal`,
  `test_idle_autostop_sb_queue`, `test_servicebus_tasks`,
  `test_service_bus_entity_counts`, `test_resident_consumer`, `test_blast_queue`,
  `test_blast_tasks`, `test_external_blast_api`, `test_blast_submit_database_retry`
  — **412 passed**.
- Local `api.log` had no `ERROR` / `5xx` / traceback entries for the run window;
  the only worker log noise is pre-existing background reconcile DNS failures
  against the fake local `elbstg01` account (graceful `errors: 1`, task
  `succeeded`), unrelated to the run.

## Live tier (not run here)

The gated live tier (real cluster stop/start, Service Bus enqueue → auto-start,
real BLAST submit + status + results, parallel fan-in) incurs cost and takes
hours (drain + ~1h capacity wait per BLAST), so it is wired + documented but not
executed in this change. Run it with the flags above against a prepared
deployment, then inspect App Insights for the window for `exceptions` / `5xx`.
