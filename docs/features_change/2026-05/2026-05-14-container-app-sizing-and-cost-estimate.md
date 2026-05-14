# 2026-05-14 — CPU/memory sizing methodology and cost estimate

## Motivation

The user asked two related questions:

1. How is CPU and memory allocated to a Container App?
2. What will the bundled Container App cost?

The migration plan now answers both with a dedicated section, including a
sized table per sidecar and a cost computation derived from
**Azure Retail Prices API for koreacentral, May 2026** (not estimated from
memory).

## User-facing change

None at runtime. Operators reading the plan now see:

- The Container Apps allocation rules (per-container minimum, increment step,
  the 1 vCPU : 2 GiB replica-total ratio, per-replica caps).
- A per-sidecar initial allocation with the reasoning behind it.
- A growth path if the bundle outgrows the 4 vCPU / 8 GiB Consumption
  per-replica cap.
- A cost computation with the actual confirmed unit prices.
- A comparison to today's Function App + SWA setup and to the earlier
  multi-app + VM revision of the migration plan.

## Confirmed unit prices (koreacentral, USD, retrieved 2026-05-14)

| Meter | Unit price |
|-------|------------|
| Standard vCPU Active Usage | `$0.000024` / vCPU-second |
| Standard vCPU Idle Usage | `$0.000003` / vCPU-second |
| Standard Memory Active and Idle Usage | `$0.000003` / GiB-second |
| Standard Requests | `$0.40` per 1,000,000 requests |
| Dedicated Plan Management (workload-profile environment fee) | `$0.10` / hour ≈ `$72` / month |
| Dedicated vCPU Usage | `$0.057077` / hour |
| Dedicated Memory Usage | `$0.004978` / GiB-hour |

Source: `mcp_azure_mcp_pricing.pricing_get` filtered by
`serviceName eq 'Azure Container Apps' and priceType eq 'Consumption'`,
region `koreacentral`.

Note: the Memory meter is the same price for active and idle; only vCPU has a
separate idle rate.

## Initial allocation per sidecar

| Sidecar | vCPU | Memory | Reasoning summary |
|---------|------|--------|-------------------|
| `frontend` | 0.25 | 0.5 GiB | Static-file nginx |
| `api` | 0.5 | 1.0 GiB | HTTP + WebSocket terminal proxy + streaming upload/download proxy with semaphore=4 |
| `worker` | 0.5 | 1.0 GiB | Celery worker; CPU spikes on ARM/AKS calls |
| `beat` | 0.25 | 0.5 GiB | Celery beat scheduler |
| `redis` | 0.25 | 0.5 GiB | Single-node broker, AOF every-second |
| `terminal` | 0.5 | 1.0 GiB | Bash + tmux + elastic-blast toolchain |
| **Replica total** | **2.25** | **4.5 GiB** | Satisfies 1 vCPU : 2 GiB ratio; within the 4 / 8 Consumption-profile per-replica cap |

## Cost summary (5% active duty cycle, realistic)

| Topology | Approx monthly cost (control-plane, KR Central) |
|----------|--------------------------------------------------|
| Today (Function App Consumption + SWA Standard) | $10 – $15 |
| Earlier multi-app + Redis VM + Terminal VM + SWA + Service Bus + Cosmos | ~$385 |
| Previous 5-sidecar bundle + SWA | ~$140 |
| **Current 6-sidecar bundle (this plan)** | **~$132** |

That is `+$120 / month` vs today, in exchange for day-1 private storage,
no browser SAS, no SSH/VM, and a single billable Azure resource.

It is `−$253 / month` vs the earlier multi-app + VM revision.

## Files changed

- `docs/container-apps-migration.md`: new top-level sections **"CPU and
  Memory Sizing"** and **"Cost Estimate (Korea Central, USD, monthly)"**
  inserted between "Resources to Create" and "Storage Network Isolation".
- This change note.

No code or infra is changed.

## Validation evidence

- Unit prices come from the Azure Retail Prices API (`koreacentral`,
  `Azure Container Apps` service) and are reproduced in the doc.
- Math (vCPU-seconds, GiB-seconds, free-grant subtraction, per-cycle dollar
  amounts) is shown step-by-step in the doc so reviewers can spot-check.
- Two scenarios are presented (Workload-profile plan + Consumption-only) so
  the cost difference of the day-1 private-storage requirement is explicit.
