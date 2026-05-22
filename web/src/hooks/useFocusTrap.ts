import { useEffect, useRef } from "react";

/**
 * Traps keyboard focus inside a container element while active.
 * Returns a ref to attach to the container.
 */
export function useFocusTrap<T extends HTMLElement = HTMLDivElement>(active: boolean) {
  const ref = useRef<T>(null);

  useEffect(() => {
    if (!active || !ref.current) return;

    const container = ref.current;
    const focusable = () =>
      container.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])',
      );

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const els = focusable();
      if (els.length === 0) return;
      const first = els[0];
      const last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    // Focus first focusable element — but only if focus isn't already
    // inside the container (e.g. an `autoFocus` input has just claimed it).
    // Without this guard the trap clobbers any deliberate initial focus.
    const els = focusable();
    if (els.length > 0 && !container.contains(document.activeElement)) {
      els[0].focus();
    }

    container.addEventListener("keydown", handleKeyDown);
    return () => container.removeEventListener("keydown", handleKeyDown);
  }, [active]);

  return ref;
}
