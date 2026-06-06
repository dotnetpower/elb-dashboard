/**
 * diagnostics — typed client for `GET /api/diagnostics/{category}`.
 *
 * Read-only Reliability / Availability best-practice findings over the
 * configured Azure resources. Unlike the monitor endpoints, this surface does
 * not degrade open: a fetch failure / permission denial arrives as an
 * `indeterminate` finding, so "could not check" is never rendered as "ok".
 *
 * The `Severity` / `DiagnosticCategory` unions mirror the backend
 * `api/services/diagnostics/models.py`. They are intentionally open at the edge
 * — `severityRank` falls back for an unknown value so a newer backend that adds
 * a severity does not break an older SPA.
 */
import { api } from "@/api/client";

export type Severity = "ok" | "info" | "warning" | "critical" | "indeterminate";
export type DiagnosticCategory = "reliability" | "availability";
export type ResourceKind =
  | "aks"
  | "storage"
  | "acr"
  | "container_app"
  | "api"
  | "queue";

export interface Finding {
  id: string;
  category: DiagnosticCategory;
  pillar: string;
  resource_kind: ResourceKind;
  resource_name: string;
  severity: Severity;
  title: string;
  detail: string;
  recommendation: string;
  doc_url: string;
  rule_version: string;
  expected_by_charter: boolean;
  observed: Record<string, string>;
}

export interface DiagnosticReport {
  category: DiagnosticCategory;
  generated_at: string;
  findings: Finding[];
  rollup: Record<string, number>;
  has_indeterminate: boolean;
}

const SEVERITY_ORDER: Record<string, number> = {
  critical: 4,
  indeterminate: 3,
  warning: 2,
  info: 1,
  ok: 0,
};

/** Sort key for a severity string (unknown values sort lowest). */
export function severityRank(severity: string): number {
  return SEVERITY_ORDER[severity] ?? -1;
}

export interface DiagnosticTargetParams {
  subscriptionId: string;
  workloadResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  storageAccountName?: string;
  region?: string;
}

function querystring(params: Record<string, string>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) usp.set(k, v);
  }
  return usp.toString();
}

export const diagnosticsApi = {
  report: (
    category: DiagnosticCategory,
    target: DiagnosticTargetParams,
    fresh = false,
  ) => {
    const qs = querystring({
      subscription_id: target.subscriptionId,
      workload_resource_group: target.workloadResourceGroup ?? "",
      acr_resource_group: target.acrResourceGroup ?? "",
      acr_name: target.acrName ?? "",
      storage_account_name: target.storageAccountName ?? "",
      region: target.region ?? "",
      fresh: fresh ? "true" : "",
    });
    return api.get<DiagnosticReport>(`/diagnostics/${category}?${qs}`);
  },
};
