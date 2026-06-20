/**
 * Age-sorted, lazily-rendered slice of a cluster workload list.
 *
 * Single-responsibility: take a workload snapshot (Pods / Jobs), order it
 * newest-first by the Kubernetes `age` (creationTimestamp), and expose only a
 * growing prefix so a 500+ row roster does not render at once. Shared by the
 * Pods and Jobs panels so both behave identically. No I/O, no presentation.
 *
 * The caller wires `scrollRef` onto the vertical scroll container and
 * `sentinelRef` onto a 1px marker placed after the last rendered row; when the
 * marker scrolls into view the visible window grows by `pageSize`.
 */
import { useEffect, useMemo, useRef, useState } from "react";

export const WORKLOAD_PAGE_SIZE = 20;

/** Parse a K8s creationTimestamp into epoch millis; unparseable → -Infinity so
 *  rows without a usable age sink to the bottom instead of corrupting order. */
function ageMillis(age: string | null | undefined): number {
  if (!age) return -Infinity;
  const t = Date.parse(age);
  return Number.isNaN(t) ? -Infinity : t;
}

export interface AgeSortedInfinite<T> {
  /** Full list ordered newest-first (largest creationTimestamp first). */
  sorted: T[];
  /** The currently-rendered prefix of `sorted` (length ≤ `total`). */
  visible: T[];
  /** True while more rows remain beyond `visible`. */
  hasMore: boolean;
  /** `sorted.length` — total rows after sorting. */
  total: number;
  /** Vertical scroll container; used as the IntersectionObserver root. */
  scrollRef: React.RefObject<HTMLDivElement>;
  /** 1px marker after the last row; intersection grows the window. */
  sentinelRef: React.RefObject<HTMLDivElement>;
}

export function useAgeSortedInfinite<T extends { age?: string | null }>(
  items: T[],
  pageSize: number = WORKLOAD_PAGE_SIZE,
): AgeSortedInfinite<T> {
  // Newest first: larger creationTimestamp at the top.
  const sorted = useMemo(
    () => [...items].sort((a, b) => ageMillis(b.age) - ageMillis(a.age)),
    [items],
  );

  const [visibleCount, setVisibleCount] = useState(pageSize);

  // Collapse back to the first page whenever the underlying list changes
  // (namespace filter switch, or a refetch that returns a different snapshot)
  // so the user is not silently parked deep in a stale tail.
  useEffect(() => {
    setVisibleCount(pageSize);
  }, [sorted, pageSize]);

  const visible = useMemo(() => sorted.slice(0, visibleCount), [sorted, visibleCount]);
  const hasMore = visibleCount < sorted.length;

  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!hasMore) return;
    const sentinel = sentinelRef.current;
    if (!sentinel) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisibleCount((c) => Math.min(c + pageSize, sorted.length));
        }
      },
      { root: scrollRef.current ?? null, rootMargin: "80px" },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [hasMore, pageSize, sorted.length]);

  return { sorted, visible, hasMore, total: sorted.length, scrollRef, sentinelRef };
}
