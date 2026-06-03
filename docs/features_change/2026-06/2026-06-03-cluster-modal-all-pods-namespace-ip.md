---
title: Cluster details modal shows all pods with namespace filter and IPs
date: 2026-06-03
tags:
  - ui
  - architecture
---

# Cluster details — all pods, namespace filter, Node / Pod IP

## Motivation

The cluster details modal's pods table only listed non-succeeded pods (the
backend applied a `status.phase!=Succeeded` Kubernetes field selector) and was
labelled "Active Pods". Operators could not see completed BLAST job pods or
filter by namespace, and there was no way to tell which IP a pod had been
assigned. The Azure portal Pods view shows every phase, a namespace filter, and
Node / Pod IP columns — this aligns the dashboard with that behaviour.

## User-facing change

- The pods section is now titled **"Pods"** and lists pods of **every phase**
  (Pending, Running, Succeeded/Completed, Failed) across all namespaces.
- A **namespace filter** dropdown (default "All namespaces") lets the operator
  scope the table; each option shows its pod count.
- A new **POD IP** column shows the CNI-assigned pod IP. The **NODE** cell now
  carries a tooltip with the full node name and node IP (`status.hostIP`).

## API / IaC diff summary

- `api/services/k8s/monitoring.py` — `k8s_get_pods` no longer sends the
  `fieldSelector=status.phase!=Succeeded` param (returns all phases) and now
  includes `pod_ip` (`status.podIP`) and `node_ip` (`status.hostIP`) per pod.
  Consumers unaffected: `capacity_signals` only counts `status == "pending"`;
  the kubectl-emulation route already mirrors `kubectl get pods`.
- `web/src/api/monitoring.ts` — `K8sPod` gains optional `pod_ip` / `node_ip`.
- `web/src/components/ClusterDiagnostics/K8sPodsSection.tsx` — namespace filter,
  POD IP column, node-IP tooltip, "Pods" label, count badge over all pods.

No IaC change. The shared user-assigned MI already reads pods via the cluster
kubeconfig token (no new permission).

## Validation evidence

- `uv run pytest -q api/tests/test_k8s_get_pods.py api/tests/test_blast_capacity_signals.py`
  → 10 passed (new `test_k8s_get_pods.py` asserts all-phases, no field selector,
  `pod_ip`/`node_ip`, namespace URL scoping).
- `uv run ruff check api/services/k8s/monitoring.py api/tests/test_k8s_get_pods.py`
  → All checks passed.
- `cd web && npx tsc --noEmit` → clean; `npx eslint K8sPodsSection.tsx monitoring.ts`
  → no errors/warnings.
