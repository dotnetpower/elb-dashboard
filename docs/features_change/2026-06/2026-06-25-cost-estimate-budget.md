---
title: Cluster cost estimate + budget guardrail
description: An approximate compute-cost estimate (node SKU price × runtime) and a configurable per-cluster monthly budget threshold with an over-budget warning on the dashboard — no Azure billing API dependency.
tags:
  - operate
  - ui
---

# Cluster cost estimate + budget guardrail

## Motivation

Operations-readiness checklist section 5: "cost/quota guardrails + budget alerts".
The dashboard had no cost surface at all. This adds the **light, honest** version:
a coarse compute-cost *estimate* and a per-cluster monthly budget threshold with a
dashboard warning — deliberately **not** a full Azure Cost Management integration.

## User-facing change

* A new **Cost estimate** card in the dashboard Resource plane shows the managed
  cluster's approximate **hourly** and **projected-monthly** compute cost
  (node SKU price × node count), plus an accrued "this session" figure when the
  cluster uptime is known.
* A **monthly budget** input lets an operator set a USD threshold; the card shows
  an over-budget warning when the projected monthly cost exceeds it.
* Everything is labelled **APPROX** with a disclaimer: workload node pool only,
  assumes 24/7 running, excludes Spot/reserved discounts, system pool, storage,
  and egress. Authoritative spend stays in Azure Cost Management.

## Design — honest approximation, no billing API

* **No Azure Cost Management / Consumption / Pricing SDK dependency.** The hourly
  price comes from a small **hardcoded SKU price map** in
  `api/services/cost/estimate.py` carrying a `PRICED_AS_OF` date that is surfaced
  to the UI. An unknown SKU returns `priced=False` so the card shows "estimate
  unavailable" rather than a fabricated number.
* Cluster SKU / node count / power state are read from the existing
  `get_aks_cluster_snapshot` monitoring wrapper; uptime is derived from the
  auto-stop `last_started_at` anchor (when present).
* The estimate is `is_estimate=True` and intentionally coarse: it covers the
  workload node pool only and assumes 24/7 running for the monthly projection.
* The per-cluster budget is one Azure Table row (`budgetpref`,
  `PartitionKey = "budget:" + sub:rg:cluster`), mirroring the
  performance/auto-stop preference pattern. Budget is clamped to `[0, 10M]`; 0
  means "no threshold".

### Hardening (post-critique)

* Disclaimer spells out the approximation boundaries (workload pool only / 24/7 /
  exclusions) so a low estimate is never mistaken for a bill.
* `GET /api/cost` degrades to a `degraded` payload (never 500) when the cluster
  is unreadable.
* Budget is validated both at the HTTP boundary (`Field(ge=0)`) and in
  `normalise_budget` (negative/NaN → 0, capped). Query params are length-bounded.

## API / IaC diff summary

* New backend: `api/services/cost/estimate.py` (pure price map + math),
  `api/services/cost/budget_pref.py` (Table-backed budget), `api/routes/cost.py`
  (`GET /api/cost`, `GET/PUT /api/cost/budget`, all `require_caller`), registered
  in `api/main.py`.
* New frontend: `web/src/api/cost.ts` (+ barrel),
  `web/src/components/cards/CostCard.tsx`, wired into
  `web/src/pages/Dashboard/DashboardGrid.tsx`.
* No IaC change: the `budgetpref` table is created on first use. No new env var,
  no new Azure resource, no billing-API dependency.

## Validation evidence

* `uv run pytest -q api/tests/test_cost_estimate.py api/tests/test_cost_budget_pref.py api/tests/test_cost_routes.py` — 15 passed.
* `uv run ruff check api` — all checks passed.
* `cd web && npm run build` — built successfully (resolved a `CostEstimate`/`costApi`
  export clash with `@/api/blastTools` by renaming to `ClusterCostEstimate`/`clusterCostApi`).
* `uv run pytest -q api/tests` — 4621 passed, 3 skipped, 1 failed. The single
  failure (`test_control_plane_env.py::test_bicep_references_every_guard_key`,
  `STORAGE_DATE_LAYOUT_ENABLED`) is pre-existing and unrelated — this change
  touches no `infra/` file.
