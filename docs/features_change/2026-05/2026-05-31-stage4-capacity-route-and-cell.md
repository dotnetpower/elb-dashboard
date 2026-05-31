---
title: "BLAST capacity gate Stage 4 — snapshot API + cluster bento cell"
description: "Stage 4 of issue #23 — adds /api/blast/capacity and the dashboard CapacityGateCell."
tags: [blast, ui, architecture]
---

# Stage 4 — Capacity gate snapshot route + dashboard cell

## Motivation

Stages 1–3 ship the gate logic ([2026-05-31-capacity-gate-stage1-tests.md](2026-05-31-capacity-gate-stage1-tests.md)),
per-job workdir isolation ([2026-05-31-stage2-workdir-isolation.md](2026-05-31-stage2-workdir-isolation.md)),
the cached capacity signal resolver
([2026-05-31-stage3a-capacity-signals.md](2026-05-31-stage3a-capacity-signals.md)),
and the worker wiring ([2026-05-31-stage3-submit-wiring.md](2026-05-31-stage3-submit-wiring.md)).
The gate's verdict is now observable only inside worker logs — operators have
to grep `blast_gate_admit` to know if a cluster is congested.

Stage 4 surfaces the gate so the dashboard answers *one* question without
opening a terminal: **why did submit just wait, and how many slots are
already in use?**

## User-facing change

- The cluster bento on the dashboard grows a new "Capacity Gate" cell next
  to the recent-runtime summary. It shows:
  - **State pill** (`Admitting` / `Holding` / `Preview only`) tinted by
    `capacityGateBandClass` (ok / warning / danger / disabled / degraded).
  - **Slots `N / max`** with a thin progress bar.
  - **CPU + Memory request%** vs configured watermark, with a watermark
    tick mark.
  - **Pending pods** count.
  - **decision_reason** code (e.g. `cpu_watermark`) when the gate would
    deny, and a *"Signals degraded"* warning when the K8s payload is
    missing.
- The cell uses TanStack Query with a 30 s `refetchInterval`, so it
  picks up env flips and node-pool churn without a page reload.
- When `BLAST_GATE_ENABLED=false` the cell renders the same data but
  labels itself **Preview only** and uses the muted disabled tone — that
  matches the rollout plan in Charter §12a Rule 4 where the gate ships
  default-OFF and can be enabled per environment.

## API / IaC diff summary

- **New route** `GET /api/blast/capacity` (`api/routes/blast/capacity.py`)
  guarded by `require_caller`, query params:
  `subscription_id, resource_group, cluster_name, program=blastn,
  database=nt`.
- Response shape:
  ```json
  {
    "data": {
      "enabled": false,
      "pool": "blastpool",
      "slots": { "in_use": 0, "max": 1 },
      "cpu_request_pct": 10,
      "memory_request_pct": 15,
      "watermark_cpu_pct": 75,
      "watermark_memory_pct": 75,
      "pending_pods": 0,
      "decision_preview": "admit",
      "decision_reason": null,
      "decision_retryable": false,
      "predicted_demand": { "cpu_m": 1000, "mem_mib": 4096 },
      "active_reservations": [],
      "signals_degraded": false,
      "signals_error": null
    },
    "meta": { "generated_at": "...", "warnings": [] }
  }
  ```
- Wired into `api/routes/blast/__init__.py` between the existing
  `_results_routes` and the implicit catch-all (no prefix collision —
  the blast router itself owns `/api/blast/*`).
- The route is **read-only and never raises**. K8s degradation folds
  into `signals_degraded=true` + `signals_error="<ExceptionClass>"`;
  Redis reservation lookup failures fall back to an empty list. This
  matches the existing `/api/monitor/*` "_graceful" contract used
  elsewhere on the dashboard.
- No Bicep changes — the gate already has env defaults from Stage 3c
  (`BLAST_GATE_ENABLED=false` on api/worker/beat). Operators flip the
  env per environment to enforce.

### Frontend

- `blastApi.getCapacityGate(...)` typed client + exported
  `CapacityGateSnapshot` interface in `web/src/api/blast.ts`.
- Pure helper `capacityGateBandClass(snapshot)` lives next to the
  client so vitest can import it without jsdom.
- New `CapacityGateCell` component in
  `web/src/components/cards/ClusterBento/CapacityGateCell.tsx`, wired
  into `ClusterBento` after the recent-runtime cell.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_capacity_route.py` → 6 passed
  (default-disabled admit preview, degraded signals deny, reservation
  list, missing query param 422, auth required, exception is caught).
- `cd web && npm test -- --run CapacityGateCell.test` → 7 passed
  (`capacityGateBandClass` enabled / disabled / degraded / danger /
  warning matrix).
- `cd web && npm run build` → ✓ built in 7.76s, no new TS errors.
- `uv run ruff check api/routes/blast/capacity.py
  api/tests/test_blast_capacity_route.py api/routes/blast/__init__.py`
  → All checks passed.

## Notes for Stage 5

Stage 5 (telemetry) will wire `blast_gate_admit / deny / release` into
the audit log + add lightweight in-memory counters that the
`/api/monitor/sidecars` SSE stream surfaces. The Stage 4 snapshot route
is intentionally **not** the place to emit counters — it stays
idempotent and side-effect-free.
