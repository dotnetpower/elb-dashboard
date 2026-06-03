import { useEffect, useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  AlertTriangle,
  FolderOpen,
  Loader2,
  Unlock,
} from "lucide-react";

import { api } from "@/api/client";
import { useToast } from "@/components/Toast";

interface StorageLockedPanelProps {
  subscriptionId: string;
  storageAccount: string;
  resourceGroup: string;
  jobId: string;
  /** Called ~8s after the unlock succeeds — gives RBAC + DNS time to settle. */
  onUnlocked: () => void;
}

/**
 * Shown on the Results panel when the storage account has
 * `publicNetworkAccess: Disabled` and the browser cannot list result blobs.
 *
 * Lets the user temporarily flip the network surface back open so they can
 * download results, then hands off to the parent which re-runs the listing.
 */
export function StorageLockedPanel({
  subscriptionId,
  storageAccount,
  resourceGroup,
  jobId,
  onUnlocked,
}: StorageLockedPanelProps) {
  const { toast } = useToast();
  const resultsUrl = `https://${storageAccount}.blob.core.windows.net/results/${jobId}`;
  // Hold the post-unlock handoff timer so it can be cancelled if the panel
  // unmounts within the 8s window (avoids calling onUnlocked after unmount).
  const unlockTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (unlockTimerRef.current !== null) {
        clearTimeout(unlockTimerRef.current);
      }
    },
    [],
  );

  const enableMutation = useMutation({
    mutationFn: () =>
      api.post<{ public_network_access: string | null }>(
        "/monitor/storage/public-access",
        {
          subscription_id: subscriptionId,
          resource_group: resourceGroup,
          account_name: storageAccount,
          enabled: true,
        },
      ),
    onSuccess: () => {
      toast("Storage unlocked. Loading results...", "success");
      if (unlockTimerRef.current !== null) {
        clearTimeout(unlockTimerRef.current);
      }
      unlockTimerRef.current = setTimeout(onUnlocked, 8000);
    },
    onError: (e) => toast(`Failed to enable storage: ${(e as Error).message}`, "error"),
  });

  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      <div
        style={{
          padding: "16px",
          borderRadius: 10,
          background: "rgba(240,198,116,0.06)",
          border: "1px solid rgba(240,198,116,0.18)",
        }}
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <AlertTriangle
            size={18}
            style={{ color: "var(--warning)", flexShrink: 0, marginTop: 2 }}
          />
          <div style={{ flex: 1 }}>
            <div
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: "var(--warning)",
                marginBottom: 6,
              }}
            >
              Storage public access is disabled
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
              Result files are stored in your Azure Blob Storage but cannot be listed
              while public access is off. Temporarily enable it to view and download your
              BLAST results.
            </div>
            <button
              className="glass-button glass-button--primary"
              onClick={() => enableMutation.mutate()}
              disabled={enableMutation.isPending}
              style={{
                marginTop: 12,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 13,
              }}
            >
              {enableMutation.isPending ? (
                <>
                  <Loader2 size={14} className="spin" /> Enabling...
                </>
              ) : (
                <>
                  <Unlock size={14} strokeWidth={1.5} /> Enable Storage &amp; Load
                  Results
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      <div
        style={{
          marginTop: "var(--space-3)",
          padding: "14px 16px",
          borderRadius: 10,
          background: "var(--bg-tertiary)",
          fontSize: 12,
        }}
      >
        <div
          style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}
        >
          <FolderOpen
            size={14}
            strokeWidth={1.5}
            style={{ color: "var(--text-muted)" }}
          />
          <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>
            Results Location
          </span>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "100px 1fr",
            gap: "4px 12px",
            color: "var(--text-muted)",
          }}
        >
          <span>Account</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>
            {storageAccount}
          </code>
          <span>Container</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>results</code>
          <span>Prefix</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>{jobId}/</code>
          <span>URL</span>
          <code
            style={{
              fontSize: 10,
              color: "var(--text-faint)",
              wordBreak: "break-all",
            }}
          >
            {resultsUrl}
          </code>
        </div>
      </div>
    </div>
  );
}
