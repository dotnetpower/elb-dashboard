import { useEffect, useMemo, useState } from "react";
import { Database, Loader2, Lock, Maximize2, ShieldCheck, Unlock } from "lucide-react";

import { useToast } from "@/components/Toast";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import type { BlastDbClusterTopology } from "@/components/cards/storage/BlastDbClusterConfirm";
import { BlastDbModal } from "@/components/cards/storage/BlastDbModal";
import { useBlastDb } from "@/components/cards/storage/useBlastDb";
import {
  blastDbReadinessLabel,
  blastDbReadinessTone,
  getBlastDbReadiness,
} from "@/utils/blastDbReady";

/** Maps a readiness tone to the matching pill className. */
const TONE_PILL_CLASS: Record<string, string> = {
  ok: "dv3-pill-success",
  loading: "dv3-pill-warning",
  blocked: "dv3-pill-danger",
  neutral: "dv3-pill-faint",
  accent: "dv3-pill-accent",
};

interface BlastDbSectionProps {
  subscriptionId: string;
  resourceGroup: string;
  accountName: string;
  clusterName: string;
  acrName?: string;
  clusterTopology?: BlastDbClusterTopology;
  /** Bubbles "anything in flight?" up to the parent card so it can shimmer. */
  onDownloadingChange?: (db: string | null) => void;
}

/**
 * BLAST Databases sub-section of the Storage card. Shows an inline summary
 * (downloaded chips, totals) and opens a modal where the user can manage the
 * full catalog.
 *
 * State is owned by `useBlastDb`; this file is the layout.
 */
export function BlastDbSection({
  subscriptionId,
  resourceGroup,
  accountName,
  clusterName,
  acrName,
  clusterTopology,
  onDownloadingChange,
}: BlastDbSectionProps) {
  const enabled = Boolean(subscriptionId && resourceGroup && accountName);
  const state = useBlastDb({
    subscriptionId,
    resourceGroup,
    accountName,
    clusterName,
    acrName,
    enabled,
  });
  const [showPopup, setShowPopup] = useState(false);
  const { toast } = useToast();

  // Surface in-flight downloads to the parent (StorageCard uses this for shimmer)
  useEffect(() => {
    onDownloadingChange?.(state.activeDownload);
  }, [state.activeDownload, onDownloadingChange]);

  const {
    dbQuery,
    downloadedDbs,
    updatesAvailable,
    publicAccessDisabled,
    canEnableLocalAccess,
    canGrantLocalRbac,
    openingLocalDebug,
    grantingLocalRbac,
    enableLocalAccess,
    grantLocalRbac,
    storageAccessTitle,
    storageAccessHint,
  } = state;

  const showLocalDebugBanner = publicAccessDisabled && canEnableLocalAccess;
  const showLocalRbacBanner = canGrantLocalRbac;

  // Split the catalogue map by honest readiness so the header pills never
  // count a mid-copy DB (e.g. core_nt at copy_status.phase==="copying") as
  // "downloaded". `downloadedDbs` includes every DB storage reports, ready or
  // not, so the raw size is misleading for the green pill.
  const { readyCount, downloadingCount } = useMemo(() => {
    let ready = 0;
    let downloading = 0;
    for (const meta of downloadedDbs.values()) {
      const tone = blastDbReadinessTone(getBlastDbReadiness(meta));
      if (tone === "ok") ready += 1;
      else if (tone === "loading") downloading += 1;
    }
    return { readyCount: ready, downloadingCount: downloading };
  }, [downloadedDbs]);

  return (
    <div className="dv3-db-section">
      <div className="dv3-db-section-head">
        <h4 className="title" style={{ margin: 0 }}>
          <Database size={14} strokeWidth={1.5} /> BLAST Databases
        </h4>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {dbQuery.isLoading && (
            <Loader2 size={12} className="spin" style={{ color: "var(--text-faint)" }} />
          )}
          {publicAccessDisabled && (
            <span
              className="dv3-pill dv3-pill-warning"
              title="Storage is Private only. Database list cannot be read from this local network."
            >
              <Lock size={10} strokeWidth={2} style={{ marginRight: 3 }} />
              access blocked
            </span>
          )}
          {!publicAccessDisabled && readyCount > 0 && (
            <span className="dv3-pill dv3-pill-success">{readyCount} downloaded</span>
          )}
          {!publicAccessDisabled && downloadingCount > 0 && (
            <span
              className="dv3-pill dv3-pill-warning"
              style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
            >
              <Loader2 size={10} className="spin" strokeWidth={2} />
              {downloadingCount} downloading
            </span>
          )}
          {updatesAvailable > 0 && (
            <span className="dv3-pill dv3-pill-warning">
              {updatesAvailable} update{updatesAvailable > 1 ? "s" : ""}
            </span>
          )}
          <span className="counts">
            {downloadedDbs.size}/{DB_CATALOG.length} catalog
          </span>
          <button
            className="glass-button"
            style={{ padding: "3px 6px", border: "none" }}
            onClick={() => {
              setShowPopup(true);
              dbQuery.refetch();
            }}
            title="Open database manager"
          >
            <Maximize2 size={12} strokeWidth={1.5} />
          </button>
        </div>
      </div>

      {showLocalDebugBanner && (
        <div
          style={{
            margin: "4px 4px 6px",
            padding: "8px 10px",
            background: "rgba(240,198,116,0.06)",
            border: "1px solid rgba(240,198,116,0.22)",
            borderRadius: 8,
            display: "flex",
            alignItems: "center",
            gap: 10,
            fontSize: 11,
            color: "var(--text-muted)",
            lineHeight: 1.4,
          }}
        >
          <Lock size={14} style={{ color: "var(--warning)", flexShrink: 0 }} />
          <div style={{ flex: 1 }}>
            <div style={{ color: "var(--text-primary)", fontWeight: 500 }}>
              {storageAccessTitle}
            </div>
            <div style={{ marginTop: 2 }}>{storageAccessHint}</div>
          </div>
          <button
            className="glass-button glass-button--primary"
            disabled={openingLocalDebug || !enabled}
            onClick={async () => {
              const result = await enableLocalAccess();
              toast(result.message, result.ok ? "success" : "error");
            }}
            style={{
              padding: "5px 10px",
              fontSize: 11,
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
            }}
            title="Enable local public access (IP allowlist)"
          >
            {openingLocalDebug ? (
              <Loader2 size={12} className="spin" />
            ) : (
              <Unlock size={12} strokeWidth={1.8} />
            )}
            {openingLocalDebug ? "Opening…" : "Enable for local debug"}
          </button>
        </div>
      )}

      {showLocalRbacBanner && (
        <div
          style={{
            margin: "4px 4px 6px",
            padding: "8px 10px",
            background: "rgba(224,123,138,0.06)",
            border: "1px solid rgba(224,123,138,0.22)",
            borderRadius: 8,
            display: "flex",
            alignItems: "center",
            gap: 10,
            fontSize: 11,
            color: "var(--text-muted)",
            lineHeight: 1.4,
          }}
        >
          <ShieldCheck size={14} style={{ color: "var(--danger)", flexShrink: 0 }} />
          <div style={{ flex: 1 }}>
            <div style={{ color: "var(--text-primary)", fontWeight: 500 }}>
              Storage RBAC is missing for this local session
            </div>
            <div style={{ marginTop: 2 }}>
              Grant the local API Azure credential Storage data-plane roles for local
              debugging. Azure may take a few minutes to propagate the assignment.
            </div>
          </div>
          <button
            className="glass-button glass-button--primary"
            disabled={grantingLocalRbac || !enabled}
            onClick={async () => {
              const result = await grantLocalRbac();
              toast(result.message, result.ok ? "success" : "error");
            }}
            style={{
              padding: "5px 10px",
              fontSize: 11,
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
            }}
            title="Grant Storage RBAC for this local dashboard session"
          >
            {grantingLocalRbac ? (
              <Loader2 size={12} className="spin" />
            ) : (
              <ShieldCheck size={12} strokeWidth={1.8} />
            )}
            {grantingLocalRbac ? "Granting…" : "Grant local RBAC"}
          </button>
        </div>
      )}

      {downloadedDbs.size > 0 && (
        <div
          style={{ display: "flex", gap: 4, flexWrap: "wrap", padding: "2px 4px 4px" }}
        >
          {[...downloadedDbs.entries()].map(([name, meta]) => {
            const readiness = getBlastDbReadiness(meta);
            const tone = blastDbReadinessTone(readiness);
              const pillClass = TONE_PILL_CLASS[tone] ?? "dv3-pill-faint";
            const inFlight = tone === "loading";
            return (
              <span
                key={name}
                className={`dv3-pill ${pillClass}`}
                title={readiness.ready ? name : `${name} — ${blastDbReadinessLabel(readiness)}`}
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
              >
                {inFlight && <Loader2 size={10} className="spin" strokeWidth={2} />}
                {name}
                {!readiness.ready && readiness.progress && (
                  <span style={{ opacity: 0.75, fontVariantNumeric: "tabular-nums" }}>
                    {readiness.progress.success}/{readiness.progress.total}
                  </span>
                )}
              </span>
            );
          })}
        </div>
      )}

      {showPopup && (
        <BlastDbModal
          state={state}
          clusterTopology={clusterTopology}
          onClose={() => setShowPopup(false)}
        />
      )}
    </div>
  );
}
