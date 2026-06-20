/**
 * Scroll + infinite-load chrome for the cluster Workloads tables.
 *
 * Single-responsibility: wrap a workload `<table>` in a fixed-height vertical
 * scroll container, place the IntersectionObserver sentinel after it, and show
 * a compact "N / total" footer. Shared by the Pods and Jobs panels so both get
 * identical 20-rows-at-a-time infinite scrolling. The sort + windowing logic
 * lives in `useAgeSortedInfinite`; this component is presentation only.
 */
import type { ReactNode, RefObject } from "react";

/** Max visible height of the scroll viewport — roughly one page (20 rows of
 *  the dense mono table plus the sticky-ish header) before scrolling kicks in. */
const VIEWPORT_MAX_HEIGHT = 460;

export function WorkloadScroll({
  scrollRef,
  sentinelRef,
  shown,
  total,
  hasMore,
  children,
}: {
  scrollRef: RefObject<HTMLDivElement>;
  sentinelRef: RefObject<HTMLDivElement>;
  shown: number;
  total: number;
  hasMore: boolean;
  children: ReactNode;
}) {
  return (
    <div
      ref={scrollRef}
      style={{ maxHeight: VIEWPORT_MAX_HEIGHT, overflowY: "auto", overflowX: "auto" }}
    >
      {children}
      {hasMore && <div ref={sentinelRef} aria-hidden="true" style={{ height: 1 }} />}
      <div
        style={{
          padding: "6px 8px",
          fontSize: 9,
          textAlign: "center",
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
        }}
      >
        {hasMore ? `Showing ${shown} / ${total} · scroll for more` : `${total} total`}
      </div>
    </div>
  );
}
