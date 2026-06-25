/**
 * cost — typed client for `/api/cost` (approximate estimate + budget guardrail).
 *
 * The estimate is explicitly approximate (node SKU price × runtime, hardcoded
 * price map) and must always be rendered as such — never as a bill.
 */
import { api } from "@/api/client";

export interface ClusterCostEstimate {
  sku: string;
  node_count: number;
  priced: boolean;
  hourly_usd: number;
  uptime_seconds: number | null;
  accrued_usd: number | null;
  projected_monthly_usd: number;
  priced_as_of: string;
  is_estimate: boolean;
}

export interface CostResponse {
  cluster?: { name: string; power_state: string; node_sku: string; node_count: number };
  estimate?: ClusterCostEstimate;
  budget?: { monthly_budget_usd: number; set: boolean };
  warning?: { over_budget: boolean; ratio: number } | null;
  degraded?: boolean;
  reason?: string;
}

export interface BudgetResponse {
  monthly_budget_usd: number;
  set: boolean;
}

function q(subscriptionId: string, resourceGroup: string, clusterName: string): string {
  return (
    `subscription_id=${encodeURIComponent(subscriptionId)}` +
    `&resource_group=${encodeURIComponent(resourceGroup)}` +
    `&cluster_name=${encodeURIComponent(clusterName)}`
  );
}

export const clusterCostApi = {
  get: (subscriptionId: string, resourceGroup: string, clusterName: string) =>
    api.get<CostResponse>(`/api/cost?${q(subscriptionId, resourceGroup, clusterName)}`),
  putBudget: (
    subscriptionId: string,
    resourceGroup: string,
    clusterName: string,
    monthlyBudgetUsd: number,
  ) =>
    api.put<{ monthly_budget_usd: number }>("/api/cost/budget", {
      subscription_id: subscriptionId,
      resource_group: resourceGroup,
      cluster_name: clusterName,
      monthly_budget_usd: monthlyBudgetUsd,
    }),
};
