import { useCallback, useEffect } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, Download } from "lucide-react";

import { type BlastDbCatalogItem, DB_CATALOG } from "@/components/cards/storageDbCatalog";
import { useFocusTrap } from "@/hooks/useFocusTrap";

export interface BlastDbClusterTopology {
  hasCluster: boolean | null;
  nodeCount: number | null;
  clusterName?: string | null;
  isLoading?: boolean;
  isError?: boolean;
}

interface BlastDbClusterConfirmProps {
  dbValue: string;
  isLarge: boolean;
  topology?: BlastDbClusterTopology;
  onConfirm: () => void;
  onCancel: () => void;
}

export function shouldConfirmDownloadBeforeAks(
  topology?: BlastDbClusterTopology,
): boolean {
  if (!topology) return true;
  if (topology.isLoading || topology.isError) return true;
  if (topology.hasCluster !== true) return true;
  return !topology.nodeCount || topology.nodeCount <= 0;
}

function topologyReason(topology?: BlastDbClusterTopology): string {
  if (!topology || topology.isLoading) return "AKS status is still loading.";
  if (topology.isError) return "AKS status could not be read.";
  if (topology.hasCluster !== true)
    return "No AKS workload cluster has been created yet.";
  return "The AKS workload node count is not confirmed yet.";
}

export function BlastDbClusterConfirm({
  dbValue,
  isLarge,
  topology,
  onConfirm,
  onCancel,
}: BlastDbClusterConfirmProps) {
  const db: BlastDbCatalogItem | undefined = DB_CATALOG.find(
    (item) => item.value === dbValue,
  );
  const trapRef = useFocusTrap<HTMLDivElement>(true);

  useEffect(() => {
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [onCancel]);

  const handleBackdropClick = useCallback(
    (event: React.MouseEvent) => {
      if (event.target === event.currentTarget) onCancel();
    },
    [onCancel],
  );

  return createPortal(
    <div
      className="glass-dialog-backdrop blast-db-cluster-confirm-backdrop"
      onClick={handleBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-label={`Get ${db?.label ?? dbValue} before AKS is ready?`}
      ref={trapRef}
    >
      <div
        className="glass-card glass-card--strong glass-dialog blast-db-cluster-confirm"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="blast-db-cluster-confirm__icon" aria-hidden="true">
          <AlertTriangle size={18} />
        </div>
        <h3 className="blast-db-cluster-confirm__title">
          Get {db?.label ?? dbValue} before AKS is ready?
        </h3>
        <p className="muted blast-db-cluster-confirm__copy">
          <strong>{topologyReason(topology)}</strong> The database copy can start now, but
          shard selection and node-local warmup are sized after the workload node count is
          known. Those later steps may take extra time when the cluster is created or
          refreshed.
          {isLarge && db?.size
            ? ` This database is ${db.size} and may also take hours to copy from NCBI.`
            : ""}
        </p>
        <div className="glass-dialog__actions blast-db-cluster-confirm__actions">
          <button type="button" className="glass-button" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="glass-button glass-button--primary blast-db-cluster-confirm__primary"
            onClick={onConfirm}
            aria-label={`Continue and get ${db?.label ?? dbValue}`}
          >
            <Download size={12} /> Continue and Get
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
