/**
 * ScrollShadow — wraps a horizontally-scrollable child and shows a subtle
 * edge fade on whichever side still has hidden content, so users know a wide
 * table / row can scroll sideways.
 *
 * Responsibility: Purely presentational scroll affordance. It owns the
 * overflow container and toggles `--at-start` / `--at-end` classes from the
 * child's scroll geometry; the fades themselves are CSS (`.scroll-shadow`).
 * Edit boundaries: Keep this generic — no table/domain knowledge. Consumers
 * wrap any wide content with it.
 * Key entry points: `ScrollShadow`.
 * Risky contracts: The fade gradient uses `var(--bg-secondary)` as the
 * surface colour; place it on a matching surface so the fade blends. Only
 * safe in the browser (uses ResizeObserver / scroll listener).
 * Validation: `cd web && npm run build`.
 */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

export function ScrollShadow({
  children,
  className,
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [atStart, setAtStart] = useState(true);
  const [atEnd, setAtEnd] = useState(true);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const { scrollLeft, scrollWidth, clientWidth } = el;
      setAtStart(scrollLeft <= 1);
      // When content fits (scrollWidth ≈ clientWidth) both edges read as
      // "at end" so neither fade shows.
      setAtEnd(scrollLeft + clientWidth >= scrollWidth - 1);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null;
    ro?.observe(el);
    window.addEventListener("resize", update);
    return () => {
      el.removeEventListener("scroll", update);
      ro?.disconnect();
      window.removeEventListener("resize", update);
    };
  }, []);

  const cls =
    "scroll-shadow" +
    (atStart ? " scroll-shadow--at-start" : "") +
    (atEnd ? " scroll-shadow--at-end" : "") +
    (className ? ` ${className}` : "");
  return (
    <div className={cls} style={style}>
      <div className="scroll-shadow__content" ref={ref}>
        {children}
      </div>
    </div>
  );
}
