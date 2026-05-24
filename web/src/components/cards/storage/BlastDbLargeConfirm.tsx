import { useEffect, useRef } from "react";
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
  const confirmRef = useRef<HTMLDivElement>(null);
  const startButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      confirmRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "end",
        inline: "nearest",
      });
      startButtonRef.current?.focus({ preventScroll: true });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [dbValue]);

  return (
    <div
      ref={confirmRef}
      className="blast-db-large-confirm"
    >
      <div className="blast-db-large-confirm__title">
        <AlertTriangle size={14} /> Download {db?.label ?? dbValue}?
      </div>
      <div className="muted blast-db-large-confirm__copy">
        This database is <strong>{db?.size}</strong> and may take hours to copy from
        NCBI. Ensure your storage account has sufficient space and that the control
        plane can reach it through the private endpoint.
      </div>
      <div className="blast-db-large-confirm__actions">
        <button
          ref={startButtonRef}
          type="button"
          className="glass-button glass-button--primary blast-db-large-confirm__primary"
          onClick={onConfirm}
        >
          <Download size={12} /> Start Download
        </button>
        <button type="button" className="glass-button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
