interface StorageMetaGridProps {
  region: string;
  sku: string | null;
  isHnsEnabled: boolean;
  isPublic: boolean;
}

/**
 * Compact 4-cell grid: Region · SKU · HNS · network reachability.
 * Uses the v3 dashboard token system (`dv3-cell-grid`) so the chrome is
 * consistent with the cluster + jobs cards.
 */
export function StorageMetaGrid({
  region,
  sku,
  isHnsEnabled,
  isPublic,
}: StorageMetaGridProps) {
  return (
    <div
      className="dv3-cell-grid dv3-cell-grid--4"
      style={{ marginBottom: "var(--space-3)" }}
    >
      <div className="cell">
        <span className="label">Region</span>
        <div className="value mono">{region}</div>
      </div>
      <div className="cell">
        <span className="label">SKU</span>
        <div className="value mono">{sku ?? "?"}</div>
      </div>
      <div className="cell">
        <span className="label">HNS</span>
        <div className="value">{isHnsEnabled ? "Enabled" : "Disabled"}</div>
      </div>
      <div
        className={`cell${isPublic ? " warn" : " success"}`}
        title={
          isPublic
            ? "Public endpoint is enabled. Production posture is Private only (project policy §9)."
            : "Private only. Public endpoint is disabled; data plane is reached via private endpoint."
        }
      >
        <span className="label">Network</span>
        <div className="value">{isPublic ? "Public allowed" : "Private only"}</div>
      </div>
    </div>
  );
}
