import { Navigate, useParams, useSearchParams } from "react-router-dom";

/**
 * Legacy `/blast/jobs/:jobId/analytics` route — kept for bookmark
 * compatibility. The analytics views (Descriptions / Graphic Summary /
 * Alignments / Taxonomy) now live as tabs inside the unified
 * `/blast/jobs/:jobId` page, so this component just redirects.
 */
export function BlastAnalytics() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const next = new URLSearchParams(searchParams);
  if (!next.get("tab")) next.set("tab", "descriptions");
  return (
    <Navigate
      to={`/blast/jobs/${encodeURIComponent(jobId ?? "")}?${next.toString()}`}
      replace
    />
  );
}
