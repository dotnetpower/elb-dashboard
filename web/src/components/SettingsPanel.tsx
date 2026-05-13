import { useEffect } from "react";
import { Settings, X, RotateCcw } from "lucide-react";
import { useFocusTrap } from "@/hooks/useFocusTrap";

import type { ResourceConfig } from "@/components/SetupWizard";

interface Props {
  open: boolean;
  config: ResourceConfig;
  onClose: () => void;
  onRerunWizard: () => void;
}

export function SettingsPanel({ open, config, onClose, onRerunWizard }: Props) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);

  // Close on ESC
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  const fields: Array<{ section?: string; label: string; value: string; auto?: boolean }> = [
    { label: "Subscription", value: config.subscriptionId ? config.subscriptionId.slice(0, 12) + "…" : "—" },
    { section: "Workload", label: "Resource Group", value: config.workloadResourceGroup || "—" },
    { label: "Storage Account", value: config.storageAccountName || "—", auto: true },
    { section: "Container Registry", label: "ACR Resource Group", value: config.acrResourceGroup || "—" },
    { label: "ACR Name", value: config.acrName || "—", auto: true },
    { section: "Remote Terminal", label: "Terminal RG", value: config.terminalResourceGroup || "—" },
    { label: "Terminal VM", value: config.terminalVmName || "—", auto: true },
  ];

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)",
          zIndex: 59,
        }}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Resource Settings"
        ref={trapRef}
        style={{
          position: "fixed", top: 0, right: 0, bottom: 0,
          width: "min(520px, calc(100vw - 24px))",
          background: "var(--bg-primary)", borderLeft: "1px solid var(--border-medium)",
          boxShadow: "-8px 0 32px rgba(0,0,0,0.4)", zIndex: 60,
          display: "flex", flexDirection: "column",
        }}
      >
      {/* Header */}
      <div style={{
        padding: "16px 20px", borderBottom: "1px solid var(--border-weak)",
        display: "flex", justifyContent: "space-between", alignItems: "center",
      }}>
        <h2 style={{ fontSize: 15, fontWeight: 600, margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
          <Settings size={16} strokeWidth={1.5} /> Resource Settings
        </h2>
        <button
          onClick={onClose}
          style={{
            width: 28, height: 28, borderRadius: 6, display: "flex",
            alignItems: "center", justifyContent: "center",
            color: "var(--text-faint)", cursor: "pointer",
            border: "none", background: "none",
          }}
        >
          <X size={16} />
        </button>
      </div>

      {/* Body */}
      <div style={{ padding: "16px 20px", flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 12 }}>
        {fields.map((f, i) => (
          <div key={i}>
            {f.section && (
              <div style={{
                fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase",
                letterSpacing: "0.08em", paddingTop: 8, marginBottom: 8,
                borderTop: i > 0 ? "1px solid var(--border-weak)" : "none",
              }}>
                {f.section}
              </div>
            )}
            <div>
              <div style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 3 }}>
                {f.label}
              </div>
              <div style={{
                padding: "8px 12px", background: "var(--bg-tertiary)",
                border: "1px solid var(--border-weak)", borderRadius: 6,
                fontSize: 12, fontFamily: "var(--font-mono)",
                color: f.value === "—" ? "var(--text-faint)" : "var(--text-muted)",
              }}>
                {f.value}
                {f.auto && f.value !== "—" && (
                  <span style={{ marginLeft: 8, fontSize: 10, color: "var(--text-faint)" }}>
                    (auto-discovered)
                  </span>
                )}
              </div>
            </div>
          </div>
        ))}

        <div style={{
          padding: "10px 12px", marginTop: 8,
          background: "rgba(110,159,255,0.05)", border: "1px solid rgba(110,159,255,0.12)",
          borderRadius: 6, fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5,
        }}>
          To change these settings, re-run the setup wizard.
        </div>
      </div>

      {/* Footer */}
      <div style={{ padding: "12px 20px", borderTop: "1px solid var(--border-weak)" }}>
        <button
          className="glass-button"
          style={{ width: "100%", justifyContent: "center" }}
          onClick={onRerunWizard}
        >
          <RotateCcw size={12} strokeWidth={1.5} /> Re-run Setup Wizard
        </button>
      </div>
    </div>
    </>
  );
}
