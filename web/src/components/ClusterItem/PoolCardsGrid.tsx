import type { AksClusterSummary } from "@/api/endpoints";
import { useAksSkus } from "@/hooks/useAksSkus";

export function PoolCardsGrid({
  agentPools,
}: {
  agentPools: NonNullable<AksClusterSummary["agent_pools"]>;
}) {
  // SKU table for the pool capacity readout. React Query dedupes the request
  // so all ClusterItems share the same in-flight call.
  const { skus } = useAksSkus();
  const skuByName = new Map(skus.map((s) => [s.name, s]));

  return (
    <div className="dv3-pool-grid">
      {agentPools.map((pool) => {
        const isSystem = (pool.mode ?? "").toLowerCase() === "system";
        const roleLabel = isSystem ? "SYSTEM" : "USER";
        const scale =
          pool.enable_auto_scaling &&
          pool.min_count != null &&
          pool.max_count != null
            ? `${pool.min_count}–${pool.max_count}`
            : `${pool.count ?? "?"}`;
        const sku = skuByName.get(pool.vm_size ?? "");
        const nodes = pool.count ?? 0;
        const totalCores = sku ? sku.vCPUs * nodes : null;
        const totalGiB = sku ? sku.memoryGiB * nodes : null;
        return (
          <div
            key={pool.name}
            className={`dv3-pool-card ${isSystem ? "system" : "user"}`}
            title={`${pool.name} · mode=${pool.mode ?? "?"} · os=${
              pool.os_type ?? "?"
            }${pool.enable_auto_scaling ? " · autoscale on" : ""}`}
          >
            <div className="head">
              <span className="role">{roleLabel}</span>
              <span
                className="pool-name muted"
                style={{ fontSize: 10, fontWeight: 400, opacity: 0.7 }}
              >
                {pool.name}
              </span>
            </div>
            <div className="body">
              <span className="count">{scale}</span>
              <span>×</span>
              <span className="sku">{pool.vm_size ?? "?"}</span>
            </div>
            <div className="footer">
              {sku ? (
                <>
                  {sku.vCPUs} cores · {sku.memoryGiB} GiB / node
                  {totalCores != null && totalGiB != null && nodes > 1 && (
                    <>
                      {" · "}
                      <span style={{ color: "var(--text-muted)" }}>
                        {totalCores} / {totalGiB} GiB total
                      </span>
                    </>
                  )}
                </>
              ) : (
                <>{pool.enable_auto_scaling && "autoscale enabled"}</>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
