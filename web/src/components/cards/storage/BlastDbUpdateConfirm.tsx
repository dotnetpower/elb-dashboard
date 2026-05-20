import { AlertTriangle, RefreshCw, X } from "lucide-react";

import { formatNcbiVersion } from "@/components/cards/storageDbCatalog";
import type { DownloadedDbMeta } from "@/components/cards/storage/useBlastDb";

interface BlastDbUpdateConfirmProps {
  dbValue: string;
  meta: DownloadedDbMeta | undefined;
  latestVersion: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function BlastDbUpdateConfirm({
  dbValue,
  meta,
  latestVersion,
  onConfirm,
  onCancel,
}: BlastDbUpdateConfirmProps) {
  const fromVersion = meta?.source_version
    ? formatNcbiVersion(meta.source_version)
    : "unknown";
  const toVersion = latestVersion ? formatNcbiVersion(latestVersion) : "latest";
  const shardCount = meta?.shard_sets?.length ?? 0;
  const oracleStatus = meta?.db_order_oracle?.status;

  return (
    <div
      style={{
        marginTop: "var(--space-2)",
        padding: "10px 12px",
        borderRadius: 6,
        background: "rgba(240,198,116,0.08)",
        border: "1px solid rgba(240,198,116,0.22)",
        color: "var(--text-primary)",
        display: "grid",
        gridTemplateColumns: "16px 1fr auto",
        gap: 10,
        alignItems: "start",
      }}
    >
      <AlertTriangle size={15} color="var(--warning)" style={{ marginTop: 1 }} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
          Update {dbValue} from {fromVersion} to {toVersion}?
        </div>
        <div
          className="muted"
          style={{ fontSize: 11, lineHeight: 1.45, maxWidth: 620 }}
        >
          This starts a full server-side copy from NCBI, then promotes the new
          source version only after copy initiation succeeds. Preset shard layouts
          will be rebuilt for the new generation.
          {shardCount > 0 ? ` Current ${shardCount} shard layouts become stale.` : ""}
          {oracleStatus ? ` The DB order oracle becomes stale until rebuilt.` : ""}
          Node-local warmup for the previous generation must be recreated before it
          counts as current.
        </div>
      </div>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <button
          className="glass-button glass-button--primary"
          onClick={onConfirm}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 11,
            padding: "5px 9px",
            whiteSpace: "nowrap",
          }}
          title="Start DB update"
        >
          <RefreshCw size={12} /> Update
        </button>
        <button
          className="glass-button"
          onClick={onCancel}
          style={{ padding: "5px 7px", border: "none" }}
          title="Cancel update"
        >
          <X size={13} />
        </button>
      </div>
    </div>
  );
}