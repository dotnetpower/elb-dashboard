---
title: Live VM pricing for the cost estimate (opt-in)
description: An opt-in COST_PRICING_LIVE gate that prices the cost-estimate card from the public Azure Retail Prices API (Linux on-demand, per region) with a cache and static fallback, replacing the hardcoded price map when enabled.
tags:
  - operate
  - ui
---

# Live VM pricing for the cost estimate (opt-in)

## Motivation

The cost-estimate card shipped with a **hardcoded** SKU price map — the biggest
honesty gap in that feature, since Azure prices drift and vary by region. This
closes it with an opt-in path to the public Azure Retail Prices API, keeping the
external dependency safe behind a cache, a fallback, and a default-OFF gate.

## User-facing change

* When `COST_PRICING_LIVE` is enabled, the Cost estimate card prices the cluster's
  VM SKU from the **public, no-auth Azure Retail Prices API** (Linux on-demand,
  for the cluster's region) instead of the bundled static map.
* The card's disclaimer now states which source priced the estimate ("live Retail
  prices" vs "static price map").
* Default OFF — the dashboard makes **no external call** and keeps the existing
  approximate map unless an operator opts in.

## Design

* New `api/services/cost/pricing.py`: a single module that talks to
  `prices.azure.com`. It filters strictly to **Consumption (on-demand), "1 Hour",
  non-Windows, non-Spot, non-reserved** line items and takes the lowest matching
  USD price, with a 5 s timeout and a 200-item read cap.
* **In-process TTL cache** (24h for a price, 1h negative cache for a miss/fault)
  so a dashboard poll does not hammer the API and a transient outage is not
  retried every tick.
* `estimate.py` gains a `region` arg and a `_resolve_price` that prefers live →
  static → unpriced, recording the winner in a new `priced_source` field. The
  cost route passes `snapshot["region"]`.
* SKU / region are validated against `^[A-Za-z0-9_.-]{1,64}$` before being
  interpolated into the OData filter (no injection).
* Gate documented in [feature-gates.md](feature-gates.md) per charter §12a Rule 4.

### Safety (critique + hardening)

* Live lookup is **best-effort**: any fault → `None` → static map → unpriced. The
  card never blocks on or fails because of the external call.
* Default-OFF means the existing behaviour (static map, zero external calls) is
  unchanged until explicitly enabled.
* `estimate_cluster_cost`'s new `region` arg defaults to `""`, so every existing
  caller keeps compiling and resolves to the static map.

## API / IaC diff summary

* New `api/services/cost/pricing.py`. `estimate.py` gains `region` +
  `priced_source`. `api/routes/cost.py` passes the cluster region. Frontend
  `ClusterCostEstimate` gains optional `priced_source`; the CostCard disclaimer
  reflects it. No IaC change, no new dependency (uses the already-present
  `httpx`), no secret (the Retail API is public/no-auth).

## Validation evidence

* `uv run pytest -q api/tests/test_cost_pricing.py api/tests/test_cost_estimate.py api/tests/test_cost_routes.py` — 20 passed (line-item filter incl. Windows/Spot/reserved exclusion, gate off, malformed-input reject, hit + negative cache, live/static/fallback source).
* `uv run ruff check api` — all checks passed.
* `cd web && npm run build` — built successfully.
* `uv run pytest -q api/tests` — full suite; the only failure remains the
  pre-existing, unrelated `test_control_plane_env.py::test_bicep_references_every_guard_key`.
