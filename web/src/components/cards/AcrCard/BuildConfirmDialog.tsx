import { Hammer } from "lucide-react";

export interface BuildConfirmDialogProps {
  totalCount: number;
  builtCount: number;
  onStart: () => void;
  onCancel: () => void;
}

export function BuildConfirmDialog({
  totalCount,
  builtCount,
  onStart,
  onCancel,
}: BuildConfirmDialogProps) {
  return (
    <div
      style={{
        marginTop: "var(--space-3)",
        padding: "10px 14px",
        background: "rgba(110,159,255,0.06)",
        border: "1px solid rgba(110,159,255,0.2)",
        borderRadius: 8,
        fontSize: 12,
      }}
    >
      <div
        style={{
          fontWeight: 600,
          marginBottom: 6,
          color: "var(--accent)",
        }}
      >
        <Hammer size={14} style={{ verticalAlign: "middle", marginRight: 4 }} />
        Build {totalCount} images?
      </div>
      <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>
        Images will be built from GitHub via ACR Build Tasks. Estimated time:
        ~5-15 min per image ({totalCount * 10}+ min total).
        {builtCount > 0 && ` ${builtCount} already built will be rebuilt.`}
      </div>
      <div style={{ display: "flex", gap: "var(--space-2)" }}>
        <button
          className="glass-button glass-button--primary"
          onClick={onStart}
          style={{ fontSize: 11 }}
        >
          <Hammer size={11} /> Start Build
        </button>
        <button
          className="glass-button"
          onClick={onCancel}
          style={{ fontSize: 11 }}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
