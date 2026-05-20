import { Loader2, RefreshCw } from "lucide-react";

export interface ResultsPendingPanelProps {
  title?: string;
  message?: string;
  onRefresh?: () => void;
  refreshing?: boolean;
}

export function ResultsPendingPanel({
  title = "BLAST is still running",
  message = "Final result files are being prepared. This view will update when parseable BLAST output is available.",
  onRefresh,
  refreshing = false,
}: ResultsPendingPanelProps) {
  return (
    <div className="glass-card" style={{ padding: 28, textAlign: "center" }}>
      <Loader2
        size={28}
        className="spin"
        style={{ color: "var(--accent)", marginBottom: 10 }}
      />
      <p style={{ color: "var(--text-primary)", margin: "0 0 6px", fontWeight: 600 }}>
        {title}
      </p>
      <p className="muted" style={{ margin: 0, fontSize: 12 }}>
        {message}
      </p>
      {onRefresh && (
        <button
          className="btn btn--ghost btn--sm"
          onClick={onRefresh}
          disabled={refreshing}
          style={{ marginTop: 14 }}
        >
          <RefreshCw size={14} className={refreshing ? "spin" : ""} />
          Refresh
        </button>
      )}
    </div>
  );
}