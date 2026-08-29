import { AlertTriangle, DownloadCloud, X } from "lucide-react";

import { formatBytes, formatStorageDate } from "@/components/cards/storageDbCatalog";
import type { PendingDbUpdate } from "@/components/cards/storage/useBlastDb";

export function BlastDbDirectUpdateConfirm({
  dbValue,
  pending,
  onConfirm,
  onCancel,
}: {
  dbValue: string;
  pending: PendingDbUpdate;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      style={{
        marginTop: "var(--space-2)",
        padding: "12px",
        borderRadius: 6,
        background: "rgba(240,198,116,0.08)",
        border: "1px solid rgba(240,198,116,0.22)",
        display: "grid",
        gridTemplateColumns: "16px 1fr auto",
        gap: 10,
        alignItems: "start",
      }}
    >
      <AlertTriangle size={15} color="var(--warning)" style={{ marginTop: 1 }} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
          Update {dbValue} via NCBI Direct (HTTPS)?
        </div>
        <div className="muted" style={{ fontSize: 11, lineHeight: 1.45 }}>
          NCBI published this release on {formatStorageDate(pending.publishedAt)}. It is
          not available from the faster cloud mirror yet. This downloads and verifies{" "}
          {pending.numberOfVolumes ?? "all"} database archives plus a pinned taxonomy bundle
          {pending.bytesTotal ? ` (${formatBytes(pending.bytesTotal)})` : ""} through the
          running AKS cluster. It may take hours. The current generation remains stored
          and is replaced only after every archive, the taxonomy bundle, and every shard
          layout pass validation.
        </div>
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <button
          className="glass-button glass-button--primary"
          onClick={onConfirm}
          title="Start NCBI Direct update"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 11,
            padding: "5px 9px",
            whiteSpace: "nowrap",
          }}
        >
          <DownloadCloud size={12} /> Start update
        </button>
        <button
          className="glass-button"
          onClick={onCancel}
          title="Cancel update"
          style={{ padding: "5px 7px", border: "none" }}
        >
          <X size={13} />
        </button>
      </div>
    </div>
  );
}
