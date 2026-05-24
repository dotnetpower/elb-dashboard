import { Loader2 } from "lucide-react";

import { CORE_IMAGES, SHORT_NAMES, statusLabel } from "./constants";
import type { BuildResult, BuildStatus } from "./useAcrBuilds";

export interface AcrImageRowProps {
  img: string;
  tag: string;
  buildResults: BuildResult[];
  actualTags: string[];
  buildDetail: { image: string; status?: string } | undefined;
  buildStatus: BuildStatus;
  singleBuilding: string | null;
  onToggleError: (img: string) => void;
  onBuildSingle: (img: string) => void;
}

export function AcrImageRow({
  img,
  tag,
  buildResults,
  actualTags,
  buildDetail,
  buildStatus,
  singleBuilding,
  onToggleError,
  onBuildSingle,
}: AcrImageRowProps) {
  const result = buildResults.find((r) => r.image === `${img}:${tag}`);
  const isBuilt = actualTags.includes(tag);
  const isQueued = result?.status === "scheduled" && !buildDetail;
  const isBuilding = Boolean(buildDetail);
  const shortName = SHORT_NAMES[img] || img.split("/").pop() || img;
  const isFailed = result?.status === "failed";
  const isCore = CORE_IMAGES.has(img);
  const acrStatus = buildDetail?.status;
  const liveStatus = buildDetail?.status;

  return (
    <div
      key={img}
      style={{
        display: "contents",
        opacity: isCore ? 1 : 0.65,
      }}
    >
      <div className="td repo" title={img}>
        <strong>{shortName}</strong>
        {!isCore && (
          <span
            className="muted acr-optional-tag"
            style={{ fontSize: 11, marginLeft: 4 }}
          >
            (optional)
          </span>
        )}
      </div>
      <div className="td tag">{tag}</div>
      <div className="td action">
        {isBuilt ? (
          <span className="dv3-pill dv3-pill-success">Built</span>
        ) : isQueued ? (
          <span
            style={{
              color: "var(--warning)",
              fontSize: 12,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Loader2 size={11} className="spin" /> Queued
          </span>
        ) : (isBuilding || liveStatus) && liveStatus !== "Failed" ? (
          <span
            style={{
              color:
                liveStatus === "Running" || liveStatus === "Queued"
                  ? "var(--accent)"
                  : "var(--text-muted)",
              fontSize: 12,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Loader2 size={11} className="spin" />
            {statusLabel(liveStatus || acrStatus || result?.acr_status)}
          </span>
        ) : isFailed ? (
          <button
            onClick={() => onToggleError(img)}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              padding: 0,
            }}
          >
            <span className="dv3-pill dv3-pill-danger">Failed ▾</span>
          </button>
        ) : singleBuilding === img ? (
          <span
            style={{
              fontSize: 12,
              color: "var(--accent)",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <Loader2 size={11} className="spin" /> Starting
          </span>
        ) : (
          <button
            className="glass-button glass-button--primary dashboard-hide-mobile"
            style={{ fontSize: 11, padding: "3px 9px", gap: 4 }}
            onClick={() => onBuildSingle(img)}
            disabled={
              buildStatus === "queued" ||
              buildStatus === "building" ||
              singleBuilding !== null
            }
            title={`Build ${shortName}`}
          >
            Build
          </button>
        )}
      </div>
    </div>
  );
}
