---
title: AKS top-nodes HTTPError classified as transient monitor refresh failure
description: metrics.k8s.io top-nodes 5xx HTTPErrors now degrade to a deduped one-line warning plus stale fallback instead of recording an App Insights exception row.
tags:
  - operate
---

# AKS top-nodes HTTPError transient classification (#48)

## Motivation

The background monitor cache refresh for the AKS `top-nodes` snapshot raised
`requests.exceptions.HTTPError` and logged `monitor snapshot refresh failed` with
a full stack trace, which the Azure Monitor OpenTelemetry logging exporter turns
into an App Insights exception row. The metrics.k8s.io aggregated API returns a
transient 5xx while metrics-server is restarting; `k8s_top_nodes` already
swallows 404/503, but other transient codes (500/502/504/429/408) propagate via
`raise_for_status()` and were not classified as transient.

## User-facing change

None directly. Transient AKS metrics failures stop creating App Insights
exception rows — they degrade to the existing deduped one-line warning + stale
cache fallback, exactly like the `ConnectionError` / ARM-5xx families already do.
The node card still renders (empty / stale) when metrics-server is unavailable.

## API / IaC diff summary

- `api/services/monitor_cache.py`: `_is_transient_refresh_failure` now classifies
  `requests.exceptions.HTTPError` as transient when `exc.response.status_code` is
  in `{404, 408, 429, 500, 502, 503, 504}`. A real 401/403 HTTPError stays
  non-transient so genuine auth faults still surface as a full exception row.

No infra change.

## Validation evidence

- `uv run pytest -q api/tests/test_monitor_cache.py` — 18 passed, including the
  extended `test_is_transient_refresh_failure_classifies_known_families` which
  asserts 503/500/504 HTTPError → transient and 401/403 HTTPError → non-transient.
- `uv run ruff check` clean on the touched files.
