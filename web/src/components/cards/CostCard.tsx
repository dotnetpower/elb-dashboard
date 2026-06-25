/**
 * CostCard — approximate cluster compute-cost estimate + budget guardrail.
 *
 * Resolves the managed ElasticBLAST cluster from the AKS list, then shows an
 * approximate hourly / projected-monthly cost (node SKU price × count) and an
 * over-budget warning against a configurable monthly threshold. The estimate is
 * always labelled as an approximation — authoritative spend is in Azure Cost
 * Management, never here.
 */
import { useState } from "react";
import { CircleDollarSign, TriangleAlert } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { clusterCostApi } from "@/api/cost";
import { monitoringApi } from "@/api/monitoring";
import { useToast } from "@/components/Toast";

const POLL_MS = 60_000;

function usd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `$${n.toFixed(2)}`;
}

export function CostCard({
  subscriptionId,
  resourceGroup,
}: {
  subscriptionId: string;
  resourceGroup: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const enabled = Boolean(subscriptionId);

  const clusterQuery = useQuery({
    queryKey: ["aks", subscriptionId, "for-cost"],
    queryFn: () => monitoringApi.aks(subscriptionId),
    enabled,
    refetchInterval: POLL_MS,
    retry: false,
  });

  const clusters = clusterQuery.data?.clusters ?? [];
  const managed = clusters.find((c) => c.managed_by_elb) ?? clusters[0];
  const clusterName = managed?.name ?? "";
  const clusterRg = managed?.resource_group ?? resourceGroup;

  const costQuery = useQuery({
    queryKey: ["cost", subscriptionId, clusterRg, clusterName],
    queryFn: () => clusterCostApi.get(subscriptionId, clusterRg, clusterName),
    enabled: enabled && Boolean(clusterName),
    refetchInterval: POLL_MS,
    retry: false,
  });

  const [budgetInput, setBudgetInput] = useState("");

  const putBudget = useMutation({
    mutationFn: (amount: number) =>
      clusterCostApi.putBudget(subscriptionId, clusterRg, clusterName, amount),
    onSuccess: () => {
      toast("Budget updated.", "success");
      setBudgetInput("");
      void queryClient.invalidateQueries({
        queryKey: ["cost", subscriptionId, clusterRg, clusterName],
      });
    },
    onError: (err) =>
      toast(
        `Could not save budget: ${err instanceof Error ? err.message : "unknown error"}`,
        "error",
      ),
  });

  const data = costQuery.data;
  const estimate = data?.estimate;
  const budget = data?.budget;
  const warning = data?.warning;

  const handleSaveBudget = () => {
    const amount = Number(budgetInput);
    if (!Number.isFinite(amount) || amount < 0) {
      toast("Enter a valid budget (0 to clear).", "error");
      return;
    }
    putBudget.mutate(amount);
  };

  return (
    <div
      className="glass-card"
      style={{ padding: "16px 18px", display: "flex", flexDirection: "column", gap: 12 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <CircleDollarSign size={16} strokeWidth={1.5} style={{ color: "var(--text-muted)" }} />
        <span style={{ fontSize: 14, fontWeight: 600 }}>Cost estimate</span>
        <span
          style={{
            fontSize: 10,
            fontWeight: 600,
            padding: "2px 6px",
            borderRadius: 6,
            background: "var(--bg-tertiary)",
            color: "var(--text-faint)",
          }}
        >
          APPROX
        </span>
      </div>

      {!clusterName ? (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {clusterQuery.isLoading ? "Loading cluster…" : "No managed cluster found."}
        </div>
      ) : data?.degraded ? (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Cost data unavailable ({data.reason ?? "cluster unavailable"}).
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
            <Metric label="Hourly" value={estimate?.priced ? `${usd(estimate?.hourly_usd)}/hr` : "—"} />
            <Metric
              label="Projected / month"
              value={estimate?.priced ? usd(estimate?.projected_monthly_usd) : "—"}
            />
            {estimate?.accrued_usd != null && (
              <Metric label="This session" value={usd(estimate.accrued_usd)} />
            )}
          </div>

          {estimate && !estimate.priced && (
            <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
              No price on file for SKU {estimate.sku || "unknown"} — estimate unavailable.
            </div>
          )}

          {warning?.over_budget && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: "var(--danger, #f87171)",
              }}
            >
              <TriangleAlert size={14} /> Projected monthly cost exceeds budget (
              {budget ? usd(budget.monthly_budget_usd) : "—"}).
            </div>
          )}

          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Monthly budget: {budget?.set ? usd(budget.monthly_budget_usd) : "not set"}
            </span>
            <input
              type="number"
              min={0}
              step={50}
              value={budgetInput}
              onChange={(e) => setBudgetInput(e.target.value)}
              placeholder="USD (0 = clear)"
              aria-label="Monthly budget in USD"
              style={{ width: 140, padding: "5px 8px" }}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleSaveBudget();
              }}
            />
            <button
              type="button"
              onClick={handleSaveBudget}
              disabled={putBudget.isPending || budgetInput === ""}
              style={{
                padding: "5px 12px",
                fontSize: 12,
                color: "var(--accent)",
                background: "none",
                border: "1px solid var(--border-weak)",
                borderRadius: 8,
                cursor: "pointer",
              }}
            >
              {putBudget.isPending ? "Saving…" : "Set budget"}
            </button>
          </div>

          <div style={{ fontSize: 10, color: "var(--text-faint)" }}>
            Approximate (priced as of {estimate?.priced_as_of ?? "—"}) — workload node pool only,
            assumes 24/7 running; excludes Spot/reserved discounts, the system pool, storage, and
            egress. Not a bill; see Azure Cost Management for actual spend.
          </div>
        </>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span style={{ fontSize: 11, color: "var(--text-faint)" }}>{label}</span>
      <span style={{ fontSize: 18, fontWeight: 600 }}>{value}</span>
    </div>
  );
}
