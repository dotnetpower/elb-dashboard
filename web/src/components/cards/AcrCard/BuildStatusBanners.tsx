import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";

import { formatTime } from "./constants";

export interface BuildingBannerProps {
  elapsed: number;
  singleBuilding: string | null;
}

export function BuildingBanner({
  elapsed,
  singleBuilding,
}: BuildingBannerProps) {
  return (
    <div
      style={{
        marginTop: "var(--space-3)",
        padding: "6px 10px",
        background: "rgba(110,159,255,0.06)",
        border: "1px solid rgba(110,159,255,0.15)",
        borderRadius: 6,
        fontSize: 11,
        color: "var(--accent)",
      }}
    >
      <Loader2
        size={11}
        className="spin"
        style={{
          display: "inline",
          verticalAlign: "middle",
          marginRight: 4,
        }}
      />
      {singleBuilding
        ? `Building ${singleBuilding.split("/").pop()}... ${formatTime(elapsed)}`
        : `Building via ACR... ${formatTime(elapsed)}`}
    </div>
  );
}

export interface BuildDoneBannerProps {
  elapsed: number;
}

export function BuildDoneBanner({ elapsed }: BuildDoneBannerProps) {
  return (
    <div
      style={{
        marginTop: "var(--space-3)",
        fontSize: 11,
        color: "var(--success)",
      }}
    >
      <CheckCircle2 size={11} style={{ verticalAlign: "middle" }} /> All images
      built in {formatTime(elapsed)}
    </div>
  );
}

export interface BuildErrorBannerProps {
  message: string;
}

export function BuildErrorBanner({ message }: BuildErrorBannerProps) {
  return (
    <div
      style={{
        marginTop: "var(--space-3)",
        fontSize: 11,
        color: "var(--danger)",
      }}
    >
      <AlertTriangle size={11} style={{ verticalAlign: "middle" }} /> {message}
    </div>
  );
}

export interface ServerBuildingBannerProps {
  elapsed: number;
  buildingImages: string[] | undefined;
}

export function ServerBuildingBanner({
  elapsed,
  buildingImages,
}: ServerBuildingBannerProps) {
  return (
    <div
      style={{
        marginTop: "var(--space-3)",
        padding: "6px 10px",
        background: "rgba(110,159,255,0.06)",
        border: "1px solid rgba(110,159,255,0.15)",
        borderRadius: 6,
        fontSize: 11,
        color: "var(--accent)",
      }}
    >
      <Loader2
        size={11}
        className="spin"
        style={{
          display: "inline",
          verticalAlign: "middle",
          marginRight: 4,
        }}
      />
      Building in ACR... {formatTime(elapsed)}
      {buildingImages && (
        <span style={{ marginLeft: 8, color: "var(--text-faint)" }}>
          (
          {buildingImages
            .map((s) => s.split(":")[0].split("/").pop())
            .join(", ")}
          )
        </span>
      )}
    </div>
  );
}
