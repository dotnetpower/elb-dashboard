/**
 * WarmupSection — DB cache warmup panel shown inside the AKS cluster detail modal.
 *
 * Shows which databases are already warm on the cluster nodes, and lets the user
 * start a standalone warmup for downloaded databases. Uses the warmup/start
 * orchestrator endpoint.
 */
import { useState, useEffect } from "react";
import { Flame, Loader2, RefreshCw, CheckCircle2, AlertTriangle } from "lucide-react";
import { type UseQueryResult, useQuery } from "@tanstack/react-query";

import { monitoringApi, blastApi } from "@/api/endpoints";
import type { WarmupDbInfo, WarmupStatus } from "@/api/endpoints";
import { formatApiError } from "@/api/client";

// Databases that make sense to warmup (must be downloaded to storage first)
const WARMUP_CANDIDATES = [
  { value: "16S_ribosomal_RNA", label: "16S ribosomal RNA", program: "blastn", size: "~18 MB" },
  { value: "18S_fungal_sequences", label: "18S fungal sequences", program: "blastn", size: "~3 MB" },
  { value: "ITS_RefSeq_Fungi", label: "ITS RefSeq Fungi", program: "blastn", size: "~8 MB" },
  { value: "pdbnt", label: "PDB nucleotide", program: "blastn", size: "~200 MB" },
  { value: "swissprot", label: "SwissProt", program: "blastp", size: "~300 MB" },
  { value: "core_nt", label: "Core nucleotide", program: "blastn", size: "~250 GB" },
  { value: "nt", label: "Nucleotide collection", program: "blastn", size: "~400 GB" },
  { value: "nr", label: "Non-redundant protein", program: "blastp", size: "~300 GB" },
  { value: "refseq_protein", label: "RefSeq protein", program: "blastp", size: "~100 GB" },
] as const;

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  warmupDbs?: WarmupDbInfo[];
  warmupQuery?: UseQueryResult<WarmupStatus>;
  storageAccount?: string;
  storageResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  region?: string;
  nodeSku?: string | null;
  nodeCount?: number | null;
  terminalResourceGroup?: string;
  terminalVmName?: string;
}

export function WarmupSection({
  subscriptionId,
  resourceGroup,
  clusterName,
  warmupDbs = [],
  warmupQuery,
  storageAccount,
  storageResourceGroup,
  acrResourceGroup,
  acrName,
  region,
  nodeSku,
  nodeCount,
  terminalResourceGroup,
  terminalVmName,
}: Props) {
  const [selectedDb, setSelectedDb] = useState("");
  const [warmupInstanceId, setWarmupInstanceId] = useState<string | null>(() => {
    try {
      const stored = localStorage.getItem(`elb-warmup-${clusterName}`);
      return stored || null;
    } catch {
      return null;
    }
  });
  const [startError, setStartError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // Query downloaded databases from storage (to know which are available for warmup).
  // Passing cluster topology asks the backend to attach a `warmup_plan` to each
  // DB row — Phase 1 of the warmup pipeline. Cache key includes topology so
  // this call is *not* deduped with the storage-card listing (which has no
  // plan), matching the cluster-card listing's cache key shape.
  const downloadedQuery = useQuery({
    queryKey: [
      "blast-databases-warmup",
      subscriptionId,
      storageAccount,
      storageResourceGroup,
      nodeCount ?? 0,
      nodeSku ?? "",
    ],
    queryFn: () =>
      blastApi.listDatabases(
        subscriptionId,
        storageAccount!,
        storageResourceGroup || resourceGroup,
        nodeCount && nodeCount > 0 && nodeSku
          ? { numNodes: nodeCount, machineType: nodeSku }
          : undefined,
      ),
    enabled: Boolean(subscriptionId && storageAccount),
    staleTime: 120_000,
  });
  const downloadedNames = new Set(
    (downloadedQuery.data?.databases ?? []).map((d: { name: string }) => d.name),
  );
  // Index plans by db name for O(1) lookup in the select / button.
  const planByName = new Map(
    (downloadedQuery.data?.databases ?? [])
      .filter((d) => d.warmup_plan != null)
      .map((d) => [d.name, d.warmup_plan!] as const),
  );
  const selectedPlan = selectedDb ? planByName.get(selectedDb) : undefined;
  // Block Warmup if the planner says it cannot fit. Degenerate `no_db_size` is
  // not actionable here (size missing → planner can't decide), so we let it
  // through and rely on the orchestrator's own checks.
  const selectedInfeasible =
    selectedPlan != null &&
    selectedPlan.feasible === false &&
    selectedPlan.status !== "no_db_size";

  // Poll warmup orchestrator if one is active
  const orchQuery = useQuery({
    queryKey: ["warmup-orch", warmupInstanceId],
    queryFn: () => monitoringApi.warmupOrchStatus(warmupInstanceId!),
    enabled: Boolean(warmupInstanceId),
    refetchInterval: 5_000,
    retry: 1, // fail fast on stale instance_id
  });

  // Clear stale instance_id on persistent 404/error
  useEffect(() => {
    if (orchQuery.isError && warmupInstanceId) {
      // Orchestrator not found — stale localStorage. Clear it.
      setWarmupInstanceId(null);
      try {
        localStorage.removeItem(`elb-warmup-${clusterName}`);
      } catch {
        /* */
      }
    }
  }, [orchQuery.isError, warmupInstanceId, clusterName]);

  // Clear instance ID when orchestrator finishes
  useEffect(() => {
    if (!orchQuery.data) return;
    const rs = orchQuery.data.runtime_status;
    if (rs === "Completed" || rs === "Failed" || rs === "Terminated") {
      // Keep showing for a bit, then clear
      const t = setTimeout(() => {
        setWarmupInstanceId(null);
        try {
          localStorage.removeItem(`elb-warmup-${clusterName}`);
        } catch {
          /* */
        }
        warmupQuery?.refetch();
      }, 10_000);
      return () => clearTimeout(t);
    }
  }, [orchQuery.data, clusterName, warmupQuery]);

  const handleStartWarmup = async () => {
    if (!selectedDb || !storageAccount) return;
    // Defence in depth — the button is disabled when selectedInfeasible is
    // true, but keyboard activation / future programmatic calls should also
    // refuse. Showing the planner verdict in startError gives the user the
    // same fix recommendations they would see in the inline advisory.
    if (selectedInfeasible && selectedPlan) {
      setStartError(
        `Warmup blocked by feasibility planner: ${selectedPlan.message}`,
      );
      return;
    }
    setStartError(null);
    setStarting(true);
    try {
      const candidate = WARMUP_CANDIDATES.find((c) => c.value === selectedDb);
      const resp = await monitoringApi.startWarmup({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        storage_account: storageAccount,
        storage_resource_group: storageResourceGroup || resourceGroup,
        region: region || "koreacentral",
        db: `blast-db/${selectedDb}`,
        db_display_name: selectedDb,
        program: candidate?.program || "blastn",
        aks_cluster_name: clusterName,
        machine_type: nodeSku || undefined,
        num_nodes: nodeCount || undefined,
        acr_resource_group: acrResourceGroup,
        acr_name: acrName,
        terminal_resource_group: terminalResourceGroup,
        terminal_vm_name: terminalVmName,
      });
      setWarmupInstanceId(resp.instance_id);
      try {
        localStorage.setItem(`elb-warmup-${clusterName}`, resp.instance_id);
      } catch {
        /* */
      }
    } catch (e) {
      setStartError(formatApiError(e, "warmup"));
    } finally {
      setStarting(false);
    }
  };

  const orchPhase = orchQuery.data?.custom_status?.phase;
  const orchDb = orchQuery.data?.custom_status?.db;
  const orchFinished =
    orchQuery.data?.runtime_status === "Completed" ||
    orchQuery.data?.runtime_status === "Failed";
  const orchSuccess =
    orchQuery.data?.runtime_status === "Completed" &&
    orchQuery.data?.output?.status === "succeeded";

  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      <h4
        style={{
          margin: "0 0 var(--space-2) 0",
          fontSize: 13,
          fontWeight: 600,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <Flame size={14} strokeWidth={1.5} /> DB Warmup
        {warmupQuery?.isFetching && (
          <Loader2
            size={10}
            className="spin"
            style={{ color: "var(--text-faint)" }}
          />
        )}
        <button
          className="glass-button"
          onClick={() => warmupQuery?.refetch()}
          style={{ padding: "2px 6px", border: "none", marginLeft: "auto" }}
          title="Refresh warmup status"
        >
          <RefreshCw size={12} strokeWidth={1.5} />
        </button>
      </h4>

      {/* Currently warm databases */}
      {warmupDbs.length > 0 && (
        <div style={{ marginBottom: "var(--space-2)" }}>
          <div
            style={{
              fontSize: 10,
              color: "var(--text-faint)",
              textTransform: "uppercase",
              marginBottom: 4,
            }}
          >
            Cached on nodes
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {warmupDbs.map((db) => (
              <span
                key={db.name}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  fontSize: 11,
                  padding: "3px 10px",
                  borderRadius: 10,
                  background:
                    db.status === "Ready"
                      ? "rgba(106,214,163,0.1)"
                      : db.status === "Loading"
                        ? "rgba(122,167,255,0.1)"
                        : "rgba(224,123,138,0.1)",
                  color:
                    db.status === "Ready"
                      ? "var(--success)"
                      : db.status === "Loading"
                        ? "var(--accent)"
                        : "var(--danger)",
                  border: `1px solid ${
                    db.status === "Ready"
                      ? "rgba(106,214,163,0.2)"
                      : db.status === "Loading"
                        ? "rgba(122,167,255,0.2)"
                        : "rgba(224,123,138,0.2)"
                  }`,
                }}
              >
                {db.status === "Loading" ? (
                  <Loader2 size={10} className="spin" />
                ) : db.status === "Ready" ? (
                  <CheckCircle2 size={10} strokeWidth={1.5} />
                ) : (
                  <AlertTriangle size={10} strokeWidth={1.5} />
                )}
                {db.name}
                <span style={{ opacity: 0.6, fontSize: 10 }}>
                  {db.nodes_ready}/{db.total_jobs} nodes
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Active warmup orchestrator status */}
      {warmupInstanceId && orchQuery.data && (
        <div
          style={{
            padding: "8px 12px",
            borderRadius: 8,
            marginBottom: "var(--space-2)",
            fontSize: 11,
            background: orchFinished
              ? orchSuccess
                ? "rgba(106,214,163,0.08)"
                : "rgba(224,123,138,0.08)"
              : "rgba(122,167,255,0.08)",
            border: `1px solid ${
              orchFinished
                ? orchSuccess
                  ? "rgba(106,214,163,0.2)"
                  : "rgba(224,123,138,0.2)"
                : "rgba(122,167,255,0.2)"
            }`,
            color: orchFinished
              ? orchSuccess
                ? "var(--success)"
                : "var(--danger)"
              : "var(--accent)",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          {!orchFinished && <Loader2 size={12} className="spin" />}
          {orchFinished && orchSuccess && <CheckCircle2 size={12} strokeWidth={1.5} />}
          {orchFinished && !orchSuccess && (
            <AlertTriangle size={12} strokeWidth={1.5} />
          )}
          <div>
            <strong>Warmup {orchDb ? `(${orchDb})` : ""}</strong>:{" "}
            {orchPhase === "enabling_storage"
              ? "Enabling storage access..."
              : orchPhase === "configuring"
                ? "Preparing..."
                : orchPhase === "warming_up"
                  ? `Loading DB to nodes... (${(orchQuery.data?.custom_status?.steps?.warming_up as Record<string, number> | undefined)?.ready ?? 0}/${(orchQuery.data?.custom_status?.steps?.warming_up as Record<string, number> | undefined)?.total ?? "?"})`
                  : orchPhase === "completed"
                    ? "Completed"
                    : orchPhase === "failed"
                      ? `Failed: ${orchQuery.data?.output?.error?.slice(0, 100) ?? "unknown"}`
                      : orchPhase ?? orchQuery.data.runtime_status}
          </div>
        </div>
      )}

      {/* Start warmup — DB selector */}
      {!warmupInstanceId && storageAccount && (
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <select
            className="form-input"
            value={selectedDb}
            onChange={(e) => setSelectedDb(e.target.value)}
            style={{
              fontSize: 11,
              padding: "4px 8px",
              minWidth: 180,
              flex: 1,
            }}
          >
            <option value="">Select database to warmup...</option>
            {WARMUP_CANDIDATES.map((c) => {
              const downloaded = downloadedNames.has(c.value);
              const warmDb = warmupDbs.find((d) => d.name === c.value);
              const isReady = warmDb?.status === "Ready";
              const isLoading = warmDb?.status === "Loading";
              const plan = planByName.get(c.value);
              const planBlocks =
                plan != null &&
                plan.feasible === false &&
                plan.status !== "no_db_size";
              return (
                <option
                  key={c.value}
                  value={c.value}
                  disabled={!downloaded || isReady || isLoading || planBlocks}
                  title={planBlocks ? plan!.message : undefined}
                >
                  {c.label} ({c.size})
                  {!downloaded ? " — not downloaded" : ""}
                  {isReady ? " — ready" : ""}
                  {isLoading ? " — loading..." : ""}
                  {planBlocks
                    ? ` — too large for current cluster (${plan!.status === "node_sku_too_small" ? "upgrade SKU" : "add nodes or upgrade SKU"})`
                    : ""}
                </option>
              );
            })}
          </select>
          <button
            className="btn btn--primary btn--sm"
            onClick={handleStartWarmup}
            disabled={!selectedDb || starting || selectedInfeasible}
            title={
              selectedInfeasible
                ? `Warmup not feasible: ${selectedPlan!.message}`
                : undefined
            }
            style={{ fontSize: 11, whiteSpace: "nowrap" }}
          >
            {starting ? (
              <Loader2 size={11} className="spin" />
            ) : (
              <Flame size={11} strokeWidth={1.5} />
            )}{" "}
            Warmup
          </button>
        </div>
      )}

      {/* Phase 1 — surface the planner verdict for the currently selected
          DB. Hidden when the plan is missing (no cluster topology yet) or
          when the plan is silent (`ok`). For `ok_unknown_sku` we render an
          amber notice; otherwise red with recommendations. */}
      {selectedPlan && selectedPlan.status !== "ok" && (
        <div
          role={selectedInfeasible ? "alert" : "note"}
          style={{
            marginTop: 8,
            padding: "8px 10px",
            borderRadius: 6,
            fontSize: 11,
            background: selectedInfeasible
              ? "rgba(224, 123, 138, 0.10)"
              : "rgba(240, 198, 116, 0.10)",
            border: `1px solid ${
              selectedInfeasible
                ? "rgba(224, 123, 138, 0.35)"
                : "rgba(240, 198, 116, 0.35)"
            }`,
            color: "var(--text-primary)",
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          <div style={{ fontWeight: 600 }}>
            {selectedInfeasible ? "Warmup blocked" : "Warmup advisory"}
          </div>
          <div className="muted">{selectedPlan.message}</div>
          {selectedPlan.recommendations.length > 0 && (
            <ul
              style={{
                margin: "2px 0 0 0",
                paddingLeft: 16,
                lineHeight: 1.5,
                color: "var(--text-muted)",
              }}
            >
              {selectedPlan.recommendations.map((rec, i) => (
                <li key={i}>{rec}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {startError && (
        <div
          style={{
            marginTop: 6,
            fontSize: 11,
            color: "var(--danger)",
            padding: "4px 8px",
            borderRadius: 4,
            background: "rgba(224,123,138,0.08)",
          }}
        >
          {startError}
        </div>
      )}

      {!storageAccount && (
        <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Configure a storage account in Settings to enable warmup.
        </div>
      )}
    </div>
  );
}
