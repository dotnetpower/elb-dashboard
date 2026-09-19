---
title: Explain BLAST parameter effects and cost-estimate basis
description: New Search warns about consequential parameter overrides, identifies the workload shape behind its estimate, and shares live VM prices through bounded Redis caching.
tags:
  - blast
  - ui
  - operate
---

# Explain BLAST parameter effects and cost-estimate basis

## Motivation

The New Search form defined BLAST flags but did not consistently explain how a
change could alter hit membership, sensitivity, ranking, runtime, or result
comparability. Its evidence-based compute estimate also omitted the workload
shape and pricing assumption used to calculate the displayed amount. Live
[Azure Retail Prices](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
were cached independently in each process even though the deployment already
has a shared [Redis](https://redis.io/docs/latest/) sidecar.

## User-Facing Change

- Algorithm-parameter help now describes practical consequences for hit limits,
  E-value, word size, culling, output format, gap costs, low-complexity masking,
  and additional flags.
- Consequential overrides produce at most five grouped, non-blocking caution
  chips. The
  defaults produce no warnings and the submit payload is unchanged.
- The Runtime summary now identifies the selected workload SKU and node count,
  distinguishes live from dated static on-demand pricing, and states that
  reserved or Spot pricing may be lower.

## API / IaC Diff Summary

- No route, request body, response schema, Service Bus message, or Celery queue
  contract changed.
- `COST_PRICING_LIVE=false` is now carried by the shared control-plane
  environment source into both full Bicep deployments and quick deployments.
  It remains opt-in and does not add an Azure resource.
- When `COST_PRICING_LIVE=true`, positive Retail API prices are cached for 24
  hours in best-effort ops Redis and misses for 15 minutes. A bounded process
  cache remains the hot path and fallback.
- Redis reads and writes use 250 ms socket bounds. One failure opens a 30-second
  process-local cooldown, so an unavailable sidecar never delays every pricing
  lookup. Same-key Retail fetches are single-flight and outbound fetches are
  capped at four. Static-map fallback behavior is unchanged.

## Validation Evidence

- Pricing cache tests passed: 19.
- Submit guidance tests passed: 9.
- Full backend suite passed: 5,957 tests with 4 fixture-dependent skips.
- Full frontend suite passed: 1,048 tests across 120 files.
- Ruff, the mypy debt ratchet, ESLint, the 242-operation OpenAPI contract, and
  generated TypeScript contract checks passed.
- Frontend production and strict MkDocs builds passed. `infra/main.bicep` and
  `infra/modules/containerAppControl.bicep` compiled successfully.
- Browser checks at 1280 x 900 and 390 x 844 confirmed no horizontal overflow,
  no default warning, one grouped Quick scan caution, and accessible
  consequence text on each changed control.
- Sixteen independent hardening critique rounds covered compatibility,
  concurrency, failure handling, security, accessibility, deployment parity,
  and acceptance completeness. The final three rounds reported no Medium-or-
  higher finding; only Low observability/polish notes remained.
- `azd provision --preview` was attempted but could not run because the local
  Azure session was not authenticated. No deployment or cloud mutation was
  performed.