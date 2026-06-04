---
title: Fix AKS top-nodes crash on microcore CPU metrics
description: Harden Kubernetes CPU/memory quantity parsing so kubelet microcore (`u`) values no longer crash the AKS top-nodes monitor snapshot refresh.
tags:
  - operate
  - blast
---

# Fix AKS top-nodes crash on microcore CPU metrics

## Motivation

App Insights (`appi-elb-dashboard`, prod) surfaced a recurring `ValueError`
crashing the AKS monitor snapshot refresh:

```
File "/app/api/services/k8s/metrics.py", line 123, in _parse_cpu_millicores
    return int(value) * 1000
ValueError: invalid literal for int() with base 10: '102105u'
```

The Kubernetes metrics API reports node/pod CPU usage with a unit suffix whose
precision depends on the kubelet: `n` (nanocores), `u` (microcores), or `m`
(millicores). `_parse_cpu_millicores` handled `n` and `m` but not `u`, so a
microcore reading (`102105u` = ~102m) raised, aborting the whole
`monitor:aks:top-nodes:*` snapshot loader and degrading the AKS dashboard card.

## User-facing change

- The AKS **Top Nodes** card now renders correctly when the cluster's kubelet
  reports CPU usage in microcores; it no longer intermittently degrades to the
  monitor `refresh failed` state.

## Code change summary

- [api/services/k8s/metrics.py](../../../api/services/k8s/metrics.py)
  - `_parse_cpu_millicores`: add `u` (microcore → millicore) handling, accept
    fractional bare core counts (`int(float(value) * 1000)`), and return `0`
    instead of raising on unrecognised shapes.
  - `_parse_memory_ki`: add `Ti` handling and the same defensive `0` fallback,
    so a single odd memory value cannot crash the node snapshot refresh either.
- [api/tests/test_k8s_metrics_parse.py](../../../api/tests/test_k8s_metrics_parse.py)
  (new): unit coverage for every CPU/memory unit suffix, the `102105u` crash
  repro, and the garbage-input → `0` contract.

No API/IaC surface changed.

## Validation

- `uv run pytest -q api/tests/test_k8s_metrics_parse.py api/tests/test_k8s_top_pods.py api/tests/test_blast_capacity_gate.py api/tests/test_k8s_node_pressure.py` → 66 passed.
- `uv run ruff check api/services/k8s/metrics.py` → clean.
- Source: App Insights `exceptions` query over 7 d (the only exception class
  present, 3 occurrences on 2026-06-04). Failed-request query returned none;
  slow endpoints (`/api/monitor/sidecars/events` p95 ~30 min) are by-design SSE
  streams, not defects.
