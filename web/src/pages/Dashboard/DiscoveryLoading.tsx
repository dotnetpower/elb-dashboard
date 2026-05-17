import { Loader2, Search } from "lucide-react";

export interface DiscoveryLoadingProps {
  onSkip: () => void;
}

export function DiscoveryLoading({ onSkip }: DiscoveryLoadingProps) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "60vh",
        gap: 16,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Search size={20} style={{ color: "var(--accent)" }} />
        <Loader2
          size={20}
          className="spin"
          style={{ color: "var(--accent)" }}
        />
      </div>
      <div style={{ fontSize: 14, color: "var(--text-primary)" }}>
        Discovering existing BLAST workspaces…
      </div>
      <div className="muted" style={{ fontSize: 12 }}>
        Scanning resource groups for workspace configuration
      </div>
      <button
        onClick={onSkip}
        style={{
          marginTop: 12,
          background: "none",
          border: "1px solid var(--border-medium)",
          borderRadius: 8,
          color: "var(--text-muted)",
          cursor: "pointer",
          padding: "6px 16px",
          fontSize: 12,
          transition: "all 0.15s",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.borderColor = "var(--accent)";
          e.currentTarget.style.color = "var(--accent)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.borderColor = "var(--border-medium)";
          e.currentTarget.style.color = "var(--text-muted)";
        }}
      >
        Skip discovery — set up manually
      </button>
    </div>
  );
}
