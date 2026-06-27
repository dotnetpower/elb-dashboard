---
title: Service Bus throughput observability — KQL queries + alert rules
description: Operator guide to monitor SB drain throughput, E2E latency, DLQ, and per-source job outcomes against App Insights (workspace-based, query via Log Analytics). Adds a `source` dimension to the existing `blast` customEvent so SB-origin jobs are filterable, and defines suggested Azure Monitor alert rules.
tags: [release, observe, blast, operate]
---

# SB throughput observability — App Insights / Log Analytics

## What's emitted (and what changed)

The existing `record_feature_event("blast", status=..., job_id, phase, error_code)` is
already wired on every terminal BLAST status (`completed` / `failed` / `cancelled`).
This change adds a new dimension `source` (one of `servicebus`, `dashboard`,
`external_api`) recovered from `payload.external.submission_source` via
[api/services/blast/external_jobs.py::_stored_submission_source](../../../api/services/blast/external_jobs.py).
The dimension is `None` (dropped from customDimensions) for legacy rows that
have no marker.

Affected file: [api/tasks/blast/state.py](../../../api/tasks/blast/state.py)
`_update_state` terminal hook. Telemetry path unchanged otherwise.

## Reading these queries

`appi-elb-dashboard` is workspace-based (`IngestionMode=LogAnalytics`), so the
**classic** `az monitor app-insights query --app appi-elb-dashboard …` returns
empty rows even when traffic is live. **Always query the LA workspace directly**
(see [feature-events-app-insights memory](../../../memories/repo/feature-events-app-insights.md)):

```bash
WSID=$(az monitor log-analytics workspace show \
  -g rg-elb-dashboard -n log-elb-dashboard --query customerId -o tsv)
az monitor log-analytics query -w "$WSID" --analytics-query "<KQL>" -o table
```

LA schema renames: `requests` → `AppRequests`, `traces` → `AppTraces`,
`exceptions` → `AppExceptions`, `customEvents` → `AppEvents`,
`dependencies` → `AppDependencies`.

## Throughput KQL — operator copy-paste

### 1. BLAST outcomes per hour, split by source (sustained throughput)

```kql
AppEvents
| where TimeGenerated > ago(24h)
| where Name == "blast"
| extend status  = tostring(Properties.event_status)
| extend source  = tostring(Properties.source)
| extend bucket  = bin(TimeGenerated, 1h)
| summarize count() by bucket, source, status
| order by bucket desc, source asc
```

**Expected at the 500-2000/day target**: sum across all sources ≈ 20-85/hour
(2,275/day is the measured Tier-A ceiling). A sustained gap below ~14/hour for
the `source=servicebus` row over a 6-hour window is a real drop.

### 2. E2E latency p95 (drain → completion) for SB-origin jobs

The drain stamps a placeholder row at `customEvents` time `T0`; the BLAST
terminal hook fires at `T1`. The placeholder is also recorded as the blast
event when it later reaches the terminal status, so a single `AppEvents` query
suffices — join on `job_id`:

```kql
let started =
    AppEvents
    | where TimeGenerated > ago(24h)
    | where Name == "blast"
    | extend job_id = tostring(Properties.job_id)
    | extend source = tostring(Properties.source)
    | summarize start_ts = min(TimeGenerated) by job_id, source;
let finished =
    AppEvents
    | where TimeGenerated > ago(24h)
    | where Name == "blast"
    | extend job_id = tostring(Properties.job_id)
    | extend status = tostring(Properties.event_status)
    | where status in ("completed", "failed", "cancelled")
    | summarize end_ts = max(TimeGenerated) by job_id, status;
started
| join kind=inner finished on job_id
| extend e2e_s = datetime_diff('second', end_ts, start_ts)
| where source == "servicebus"  // drop dashboard / external_api
| summarize
    n_total = count(),
    p50_s = percentile(e2e_s, 50),
    p95_s = percentile(e2e_s, 95),
    p99_s = percentile(e2e_s, 99),
    max_s = max(e2e_s)
    by bin(start_ts, 1h)
| order by start_ts desc
```

**SLO interpretation** (per the
[load test results note](2026-06-27-servicebus-throughput-load-test-results.md)):

- Steady arrival (≤ MAX_ACTIVE=4 msg/min) → `p95_s ≤ 600` (10 min) is the SLO.
- Bursty arrival above MAX_ACTIVE → `p95_s` is dominated by queue-wait
  (`(burst_size/MAX_ACTIVE) × wave_time`), not a regression. Use the per-hour
  sustained throughput from query #1 instead.

### 3. SB queue / DLQ telemetry (no custom event, dependency-side)

The dashboard already polls `/api/monitor/message-flow` → the sibling
`sb_counts` map. App Insights captures this as a dependency span on the
ServiceBusAdministration client; the active / DLQ counts flow into the
response payload, not into AppEvents. The cheapest live source is the
dashboard SPA's Message Flow card. For a long-running historical view,
schedule a periodic AppMetrics emission (next sprint — current iteration ships
KQL only).

### 4. Failure-rate KQL (per source)

```kql
AppEvents
| where TimeGenerated > ago(24h)
| where Name == "blast"
| extend status = tostring(Properties.event_status)
| extend source = tostring(Properties.source)
| summarize
    total = count(),
    failed = countif(status == "failed"),
    cancelled = countif(status == "cancelled")
    by source
| extend failure_pct = round(100.0 * failed / total, 2)
```

A `failure_pct > 5%` for `source=servicebus` over 1h is the soft alert
threshold; > 10% is the hard threshold.

## Suggested Azure Monitor alert rules

Define these against the LA workspace `log-elb-dashboard`. Each rule is a
"Log search alert" with the listed KQL, an evaluation cadence, and a window.
All thresholds derive from the Tier-A live measurements (sustained 1.58
jobs/min, 7.2 min warmed p95) and are deliberately conservative so first-cut
deployments don't page on the cold-start cycle.

| # | Rule | Window | KQL (count or `agg`) | Threshold | Severity |
|---|---|---|---|---|---|
| A1 | SB throughput drop | 6h | Sum over query #1 `count_` for `source=servicebus` | `< 80` (= ~13/h sustained, ~31% under the target) | 3 (warning) |
| A2 | SB E2E p95 regression (steady) | 1h | Query #2 `p95_s` for last bucket | `> 900` (15 min — 50% over SLO; bursts ignored implicitly because they smear across buckets) | 3 (warning) |
| A3 | DLQ delta on the request queue | 1h | `AppRequests \| where Url has "monitor/message-flow"` JSON-extract is brittle — instead poll the namespace via Azure Monitor's "ServiceBus → dead-letter messages" metric directly | `> 5` new DLQ in 1h | 2 (error) |
| A4 | Drain task error rate | 30m | `AppExceptions \| where Properties.task_name has "servicebus.drain_and_resubmit"` count | `> 5 in 30m` | 2 (error) |
| A5 | Queue depth backlog | 15m | Azure Monitor metric `ServiceBusActiveMessages` on the queue | `> 200 sustained for 15m` (= bigger than a single normal burst) | 3 (warning) |
| A6 | Cluster Stopped while queue has work | 5m | Azure Monitor metric joined with AKS `up` — operator can use the dashboard Message Flow card | `Stopped AND ActiveMessages > 0` for 5m | 2 (error) |

Rules **A3 / A5 / A6** are metric-based (Service Bus / AKS namespace metrics)
and don't need any new emission. Rules **A1 / A2 / A4** rely on the existing
customEvents + the new `source` dimension this change ships.

## Validation evidence

- `uv run pytest -q api/tests/test_feature_events.py api/tests/test_blast_tasks.py` → 158 passed.
- Existing live-validation pattern stays (the change only adds a new
  customDimension; it does not change emission cadence or the event name).
- After the next deploy and one warmed SB burst, query #1 should show new
  rows with `source=servicebus`; query #2 should report a non-empty
  `p95_s` for the matching `start_ts` bucket.

## Out of scope (deferred)

- Periodic AppMetrics scraping of the `sb_counts` payload for historical
  queue/DLQ trends — requires a small worker tick, not in this sprint.
- A dashboard chart that consumes the LA workspace query — Azure Workbook
  template is the right home, separate PR.
- The KQL is **read-only**; no new IaC. Wiring an alert rule needs a portal
  click or a Bicep `Microsoft.Insights/scheduledQueryRules` resource — also
  separate, because the rule thresholds may need a per-deployment tuning pass
  after one week of production traffic.
