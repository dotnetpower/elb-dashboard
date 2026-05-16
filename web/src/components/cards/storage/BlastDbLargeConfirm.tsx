import { AlertTriangle, Download } from "lucide-react";

import {
  type BlastDbCatalogItem,
  DB_CATALOG,
} from "@/components/cards/storageDbCatalog";

interface BlastDbLargeConfirmProps {
  dbValue: string;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Inline confirmation block shown after the user clicks "Get" / "Update" on
 * any database in the `Large` category. Forces an explicit click before we
 * kick off a multi-hour copy.
 */
export function BlastDbLargeConfirm({
  dbValue,
  onConfirm,
  onCancel,
}: BlastDbLargeConfirmProps) {
  const db: BlastDbCatalogItem | undefined = DB_CATALOG.find((d) => d.value === dbValue);
  return (
    <div
      style={{
        marginTop: "var(--space-2)",
        padding: "10px 14px",
        borderRadius: 8,
        fontSize: 12,
        background: "rgba(240,198,116,0.08)",
        border: "1px solid rgba(240,198,116,0.25)",
      }}
    >
      <div
        style={{
          color: "var(--warning)",
          fontWeight: 600,
          marginBottom: 6,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <AlertTriangle size={14} /> Download {db?.label ?? dbValue}?
      </div>
      <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>
        This database is <strong>{db?.size}</strong> and may take hours to copy from
        NCBI. Ensure your storage account has sufficient space and that public access
        is enabled.
      </div>
      <div style={{ display: "flex", gap: "var(--space-2)" }}>
        <button
          className="glass-button glass-button--primary"
          onClick={onConfirm}
          style={{ fontSize: 11 }}
        >
          <Download size={10} /> Start Download
        </button>
        <button className="glass-button" onClick={onCancel} style={{ fontSize: 11 }}>
          Cancel
        </button>
      </div>
    </div>
  );
}
