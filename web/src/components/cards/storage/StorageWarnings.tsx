import { useState } from "react";
import { AlertTriangle, ShieldAlert, X } from "lucide-react";

const HNS_DISMISSED_KEY = "elb-hns-warning-dismissed";

interface StorageWarningsProps {
  isPublic: boolean;
  isHnsEnabled: boolean | null;
}

/**
 * Two stacked warning banners shown above the storage meta grid:
 *   1. Public endpoint enabled — non-dismissible (incident-grade).
 *   2. HNS disabled — dismissible, persisted in localStorage.
 */
export function StorageWarnings({ isPublic, isHnsEnabled }: StorageWarningsProps) {
  const [hnsDismissed, setHnsDismissed] = useState(() => {
    try {
      return localStorage.getItem(HNS_DISMISSED_KEY) === "1";
    } catch {
      return false;
    }
  });

  return (
    <>
      {isPublic && (
        <div
          className="storage-warning"
          style={{
            padding: "6px 10px",
            marginBottom: "var(--space-3)",
            background: "rgba(240,198,116,0.08)",
            border: "1px solid rgba(240,198,116,0.2)",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--warning)",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <ShieldAlert size={13} strokeWidth={1.5} />
          Public endpoint is enabled — expected state is{" "}
          <strong>Private only</strong>. Investigate and remediate.
        </div>
      )}

      {isHnsEnabled === false && !hnsDismissed && (
        <div
          className="storage-warning"
          style={{
            padding: "6px 10px",
            marginBottom: "var(--space-3)",
            background: "rgba(240,198,116,0.08)",
            border: "1px solid rgba(240,198,116,0.2)",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--warning)",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <AlertTriangle size={13} strokeWidth={1.5} />
          <span style={{ flex: 1 }}>
            HNS (Data Lake Gen2) is disabled. ElasticBLAST works best with HNS
            enabled.
          </span>
          <button
            onClick={() => {
              setHnsDismissed(true);
              try {
                localStorage.setItem(HNS_DISMISSED_KEY, "1");
              } catch {
                /* noop */
              }
            }}
            style={{
              background: "none",
              border: "none",
              color: "var(--text-faint)",
              cursor: "pointer",
              padding: 2,
            }}
            title="Dismiss"
          >
            <X size={12} />
          </button>
        </div>
      )}
    </>
  );
}
