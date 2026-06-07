import { useState, type CSSProperties, type ReactNode } from "react";
import { AlertTriangle, ShieldAlert, X } from "lucide-react";

const HNS_DISMISSED_KEY = "elb-hns-warning-dismissed";

interface StorageWarningsProps {
  isPublic: boolean;
  isHnsEnabled: boolean | null;
}

const BANNER_STYLE: CSSProperties = {
  padding: "8px 10px",
  marginBottom: "var(--space-3)",
  background: "rgba(240,198,116,0.08)",
  border: "1px solid rgba(240,198,116,0.2)",
  borderRadius: 6,
  fontSize: 11,
  color: "var(--warning)",
  display: "flex",
  alignItems: "flex-start",
  gap: 8,
  lineHeight: 1.4,
};

const TEXT_COLUMN_STYLE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  flex: 1,
  minWidth: 0,
};

const SUBTEXT_STYLE: CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: 10.5,
};

interface WarningBannerProps {
  icon: ReactNode;
  title: string;
  detail: string;
  action?: ReactNode;
}

function WarningBanner({ icon, title, detail, action }: WarningBannerProps) {
  return (
    <div className="storage-warning" style={BANNER_STYLE}>
      <span style={{ display: "flex", paddingTop: 1 }}>{icon}</span>
      <div style={TEXT_COLUMN_STYLE}>
        <strong>{title}</strong>
        <span style={SUBTEXT_STYLE}>{detail}</span>
      </div>
      {action}
    </div>
  );
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
        <WarningBanner
          icon={<ShieldAlert size={13} strokeWidth={1.5} />}
          title="Public network access is enabled"
          detail="Expected: Private only. Set the Storage account to Private only in the Azure Portal (or, for a local-debug session, run scripts/dev/storage-public-access.sh off)."
        />
      )}

      {isHnsEnabled === false && !hnsDismissed && (
        <WarningBanner
          icon={<AlertTriangle size={13} strokeWidth={1.5} />}
          title="HNS (Data Lake Gen2) is disabled"
          detail="ElasticBLAST works best with HNS enabled"
          action={
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
          }
        />
      )}
    </>
  );
}
