---
title: Service Bus idle tick OpenAPI-resolution App Insights flood fix
description: Stop publish_transitions from reading a stopped cluster's elb-openapi Service IP on every idle 30s beat tick, which flooded App Insights with dependency-failure exceptions.
tags:
  - operate
  - blast
---

# Service Bus idle tick stops flooding App Insights

## Motivation

An App Insights error hunt against `appi-elb-dashboard` (moonchoi
`rg-elb-dashboard`) found one dominant, runaway exception source: the
`elb-worker` role logged **1793 `requests.exceptions.ConnectionError`
(`NameResolutionError`)** in 24 hours — exactly one every 30 seconds —
all targeting a single dead AKS API-server FQDN:

```
HTTPSConnectionPool(host='elb-cluster-01-7r8qy09j.hcp.koreacentral.azmk8s.io',
port=443): Max retries exceeded with url:
/api/v1/namespaces/default/services/elb-openapi
(Caused by NameResolutionError(... Failed to resolve ...))
```

The 30-second cadence matched the `servicebus-publish-transitions` beat
tick. Root cause: `publish_transitions` resolved the OpenAPI client kwargs
(`_openapi_kwargs(cfg)`) **unconditionally, before** checking whether there
was any work to do. That resolution calls
`k8s_get_service_ip(..., "elb-openapi")`, which reads the configured
cluster's Service object from the Kubernetes API. When that cluster is
stopped (or was recreated with a new API-server FQDN), the read raises a
`requests` ConnectionError that the OpenTelemetry `requests` instrumentation
auto-records as an App Insights dependency-failure exception — even though
the function itself swallows the error and returns `None`. With zero active
bridges the tick had no reason to touch the cluster at all, so every idle
tick produced one pure-noise exception.

## User-facing change

No behaviour change for operators. The flood of dependency-failure
exceptions in App Insights / Log Analytics stops: an idle
`publish_transitions` tick (Service Bus enabled, no active bridges) now
touches only the local tracking store and never reads the cluster.

## API/IaC diff summary

### Backend (`api/`)

* `api/tasks/servicebus/tasks.py::publish_transitions`:
  * Fetch `list_active_bridges(...)` **before** resolving `_openapi_kwargs`.
  * Return early (`{"scanned": 0, "published": 0, "finished": 0,
    "errors": 0}`) when there are no active bridges, so the idle path never
    resolves the OpenAPI client kwargs and never reads the cluster's
    `elb-openapi` Service IP.
  * The non-idle path is unchanged (kwargs resolved once, then the bridge
    loop runs exactly as before).

### Tests

* `api/tests/test_servicebus_tasks.py::test_publish_transitions_idle_skips_openapi_resolution`
  — monkeypatches `_openapi_kwargs` and asserts it is **not** called on an
  idle tick, and that the return shape matches the loop-path shape.

## Validation evidence

* `uv run pytest -q api/tests/test_servicebus_tasks.py` → 25 passed.
* `uv run pytest -q api/tests/ -k "servicebus or external_blast"` →
  142 passed.
* `uv run ruff check api/tasks/servicebus/tasks.py
  api/tests/test_servicebus_tasks.py` → All checks passed.

## Other App Insights findings (triaged, no code change)

The same hunt classified the remaining error rows as expected /
already-handled, not code defects:

* `monitor:aks:top-nodes` HTTPError and `k8s_warmup_status failed`
  (worker/api) — monitoring a stopped cluster; already deduped via the
  monitor-cache / `dedup_log_warning` paths (only 3 warmup warnings in 24h
  vs the 1793 raw exceptions).
* `register-external-job` 503/401, `jobs/{id}/file` 503/404,
  `aks/openapi/deployment` 502, NCBI `genbank` 503 — by-design degraded /
  auth / external-dependency responses surfaced through the structured
  error + `_graceful` design, not unhandled failures.
* `POST|GET /{full_path:path}` 404 — Container Apps internal health probes
  (`https://100.100.x.x:8080/`, `/runtime-config.js`) and stale-revision
  artefacts, not a frontend routing bug.
