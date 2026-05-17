import type { ResourceConfig } from "@/components/SetupWizard";

import type { DiscoveredWorkspace } from "./useWorkspaceDiscovery";

export interface WorkspacePickerProps {
  workspaces: DiscoveredWorkspace[];
  onPick: (config: ResourceConfig) => void;
  onSetupNew: () => void;
}

export function WorkspacePicker({
  workspaces,
  onPick,
  onSetupNew,
}: WorkspacePickerProps) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "60vh",
        gap: 20,
        padding: "0 32px",
      }}
    >
      <div>
        <h2
          style={{
            fontSize: 18,
            fontWeight: 700,
            textAlign: "center",
            margin: 0,
          }}
        >
          BLAST Workspaces Found
        </h2>
        <div
          className="muted"
          style={{ fontSize: 12, textAlign: "center", marginTop: 4 }}
        >
          {workspaces.length} existing workspaces detected. Choose one to
          continue.
        </div>
      </div>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 8,
          width: "100%",
          maxWidth: 480,
        }}
      >
        {workspaces.map((ws) => (
          <button
            key={`${ws.config.subscriptionId}/${ws.rgName}`}
            onClick={() => onPick(ws.config)}
            className="glass-card"
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 4,
              padding: "14px 18px",
              border: "1px solid var(--border-medium)",
              borderRadius: 10,
              background: "var(--glass-bg)",
              cursor: "pointer",
              textAlign: "left",
              transition: "border-color 0.15s, background 0.15s",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = "var(--accent)";
              e.currentTarget.style.background = "var(--glass-bg-strong)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = "var(--border-medium)";
              e.currentTarget.style.background = "var(--glass-bg)";
            }}
          >
            <div
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: "var(--text-primary)",
              }}
            >
              {ws.rgName}
            </div>
            <div
              className="muted"
              style={{
                fontSize: 11,
                display: "flex",
                gap: 12,
                flexWrap: "wrap",
              }}
            >
              {ws.config.storageAccountName && (
                <span>Storage: {ws.config.storageAccountName}</span>
              )}
              {ws.config.acrName && <span>ACR: {ws.config.acrName}</span>}
              <span>Region: {ws.config.region}</span>
            </div>
          </button>
        ))}
      </div>
      <button
        onClick={onSetupNew}
        style={{
          background: "none",
          border: "none",
          color: "var(--accent)",
          cursor: "pointer",
          fontSize: 12,
          marginTop: 4,
        }}
      >
        Or set up a new workspace →
      </button>
    </div>
  );
}
