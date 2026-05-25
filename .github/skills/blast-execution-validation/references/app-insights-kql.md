# App Insights Error Hunt

Use these checks after live submit or full Azure scenarios. Prefer a four-hour window for autonomous runs unless the user requested a different range.

## Resolve Resources

Load known deployment values first:

```bash
eval "$(azd env get-values 2>/dev/null | sed 's/^/export /')"
az account show --query '{subscription:id, tenant:tenantId, user:user.name}' -o json
```

Resolve an Application Insights component and workspace when the names are not already known:

```bash
APP_INSIGHTS_ID="$(az resource list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --resource-type Microsoft.Insights/components \
  --query '[0].id' -o tsv --only-show-errors)"

WORKSPACE_ID="$(az resource show --ids "$APP_INSIGHTS_ID" \
  --query 'properties.WorkspaceResourceId' -o tsv --only-show-errors)"
```

If `WORKSPACE_ID` is empty, fall back to the App Insights query command with the component app id:

```bash
APP_ID="$(az resource show --ids "$APP_INSIGHTS_ID" \
  --query 'properties.AppId' -o tsv --only-show-errors)"
```

## Workspace-Based Queries

Use Log Analytics for workspace-based Application Insights resources:

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query '
AppRequests
| where TimeGenerated > ago(4h)
| where Success == false or ResultCode startswith "5"
| project TimeGenerated, AppRoleName, OperationName, ResultCode, Url, DurationMs, OperationId
| order by TimeGenerated desc
| take 100
' -o table
```

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query '
AppExceptions
| where TimeGenerated > ago(4h)
| project TimeGenerated, AppRoleName, OperationName, ProblemId, Type, OuterMessage, OperationId
| order by TimeGenerated desc
| take 100
' -o table
```

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query '
AppTraces
| where TimeGenerated > ago(4h)
| where SeverityLevel >= 3 or Message has_any ("ERROR", "Exception", "Traceback", "failed")
| project TimeGenerated, AppRoleName, SeverityLevel, Message, OperationId
| order by TimeGenerated desc
| take 100
' -o table
```

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query '
AppDependencies
| where TimeGenerated > ago(4h)
| where Success == false
| project TimeGenerated, AppRoleName, Target, DependencyType, Name, ResultCode, DurationMs, OperationId
| order by TimeGenerated desc
| take 100
' -o table
```

## Classic App Insights Queries

Use these if the workspace tables are unavailable:

```bash
az monitor app-insights query --app "$APP_ID" --analytics-query '
requests
| where timestamp > ago(4h)
| where success == false or resultCode startswith "5"
| project timestamp, cloud_RoleName, name, resultCode, url, duration, operation_Id
| order by timestamp desc
| take 100
' -o table
```

```bash
az monitor app-insights query --app "$APP_ID" --analytics-query '
exceptions
| where timestamp > ago(4h)
| project timestamp, cloud_RoleName, operation_Name, problemId, type, outerMessage, operation_Id
| order by timestamp desc
| take 100
' -o table
```

```bash
az monitor app-insights query --app "$APP_ID" --analytics-query '
traces
| where timestamp > ago(4h)
| where severityLevel >= 3 or message has_any ("ERROR", "Exception", "Traceback", "failed")
| project timestamp, cloud_RoleName, severityLevel, message, operation_Id
| order by timestamp desc
| take 100
' -o table
```

## Interpretation Rules

- Treat `api`, `worker`, and `beat` roles separately. A clean API with worker exceptions is still a failed execution run.
- Correlate by request id, `operation_Id`, job id, task id, and `external_correlation_id`.
- Redact subscription ids, UPNs, bearer tokens, SAS signatures, and long URLs from the final report.
- Browser-side exceptions are useful, but live BLAST success requires server-side request/task telemetry to be clean.
- If Application Insights is not configured, verify `/api/health` telemetry status and report the observability gap rather than inventing evidence.