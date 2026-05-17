import { AcrImageRow } from "./AcrImageRow";
import type { AcrBuildsState } from "./useAcrBuilds";

export interface AcrImageTableProps {
  expectedImages: [string, unknown][];
  actualTags: Record<string, string[]> | undefined;
  buildDetails: { image: string; status?: string }[] | undefined;
  buildResults: AcrBuildsState["buildResults"];
  buildStatus: AcrBuildsState["buildStatus"];
  singleBuilding: AcrBuildsState["singleBuilding"];
  onToggleError: (img: string) => void;
  onBuildSingle: (img: string) => void;
}

export function AcrImageTable({
  expectedImages,
  actualTags,
  buildDetails,
  buildResults,
  buildStatus,
  singleBuilding,
  onToggleError,
  onBuildSingle,
}: AcrImageTableProps) {
  return (
    <div className="dv3-acr-table">
      <div className="th">Image</div>
      <div className="th" style={{ textAlign: "center" }}>
        Version
      </div>
      <div className="th" style={{ textAlign: "right" }}>
        Status
      </div>
      {expectedImages.map(([img, tag]) => (
        <AcrImageRow
          key={img}
          img={img}
          tag={tag as string}
          buildResults={buildResults}
          actualTags={actualTags?.[img] ?? []}
          buildDetail={buildDetails?.find(
            (d) => d.image === `${img}:${tag as string}`,
          )}
          buildStatus={buildStatus}
          singleBuilding={singleBuilding}
          onToggleError={onToggleError}
          onBuildSingle={onBuildSingle}
        />
      ))}
    </div>
  );
}

export interface ExpandedErrorBlockProps {
  expandedError: string | null;
  buildResults: AcrBuildsState["buildResults"];
}

export function ExpandedErrorBlock({
  expandedError,
  buildResults,
}: ExpandedErrorBlockProps) {
  if (!expandedError) return null;
  const r = buildResults.find((row) => row.image.startsWith(expandedError));
  if (!r?.error) return null;
  return (
    <div
      style={{
        marginTop: "var(--space-2)",
        padding: "6px 10px",
        background: "rgba(224,123,138,0.06)",
        border: "1px solid rgba(224,123,138,0.15)",
        borderRadius: 6,
        fontSize: 10,
        color: "var(--danger)",
        fontFamily: "var(--font-mono)",
        whiteSpace: "pre-wrap",
        maxHeight: 120,
        overflow: "auto",
      }}
    >
      {r.error}
    </div>
  );
}
