import { useEffect, useRef } from "react";
import { Loader2 } from "lucide-react";

export interface JobsLoadMoreProps {
  /** Whether the server reports more jobs beyond the loaded page. */
  hasMore: boolean;
  /** True while a larger page is being fetched (keeps the list visible). */
  isFetchingMore: boolean;
  /** Request the next page (the page hook grows the limit). */
  onLoadMore: () => void;
}

/**
 * Infinite-scroll trigger for the Recent searches list.
 *
 * When the sentinel scrolls into view (with a pre-fetch buffer) it asks the
 * page hook to load the next page. The hook grows the requested `limit` and the
 * backend returns the genuinely most-recent N plus a `page.has_more` flag, so
 * older jobs that fell outside the initial 20 come back as the user scrolls.
 * A manual button is rendered as the accessible / no-`IntersectionObserver`
 * fallback so keyboard users and SSR can still page.
 *
 * Presentation + trigger only; all data acquisition lives in
 * `useBlastJobsState` / `useScopedBlastJobs`.
 */
export function JobsLoadMore({ hasMore, isFetchingMore, onLoadMore }: JobsLoadMoreProps) {
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    if (!hasMore) return;
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting) && !isFetchingMore) {
          onLoadMore();
        }
      },
      { rootMargin: "400px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, isFetchingMore, onLoadMore]);

  if (!hasMore && !isFetchingMore) return null;

  return (
    <div
      ref={sentinelRef}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px 0",
      }}
    >
      {isFetchingMore ? (
        <span
          className="muted"
          style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 13 }}
        >
          <Loader2 size={14} className="spin" />
          Loading more searches…
        </span>
      ) : (
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={onLoadMore}
          disabled={isFetchingMore}
        >
          Load more
        </button>
      )}
    </div>
  );
}
