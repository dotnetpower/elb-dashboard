import { useEffect, useRef } from "react";

/**
 * Modal accessibility primitive: traps Tab focus inside a container while
 * active, moves initial focus into it, restores focus to the trigger on close,
 * locks background scroll (nest-safe via ref counting), and — when an `onClose`
 * is supplied — closes on Escape.
 *
 * Backward compatible: callers that pass only `active` keep the original
 * trap + initial-focus behaviour and additionally gain scroll-lock + focus
 * restoration. Pass `onClose` to also wire Escape-to-close for modals that do
 * not already have their own Escape handler.
 *
 * Returns a ref to attach to the dialog container.
 */

// Ref-counted body scroll lock so stacked/nested modals don't release the lock
// when an inner modal unmounts while an outer one is still open.
let scrollLockCount = 0;
let savedOverflow = "";
let savedPaddingRight = "";

function lockBodyScroll() {
  if (typeof document === "undefined") return;
  if (scrollLockCount === 0) {
    const body = document.body;
    savedOverflow = body.style.overflow;
    savedPaddingRight = body.style.paddingRight;
    // Compensate for the now-hidden scrollbar so the page doesn't shift.
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;
    if (scrollbarWidth > 0) {
      body.style.paddingRight = `${scrollbarWidth}px`;
    }
    body.style.overflow = "hidden";
  }
  scrollLockCount += 1;
}

function unlockBodyScroll() {
  if (typeof document === "undefined") return;
  scrollLockCount = Math.max(0, scrollLockCount - 1);
  if (scrollLockCount === 0) {
    document.body.style.overflow = savedOverflow;
    document.body.style.paddingRight = savedPaddingRight;
  }
}

export function useFocusTrap<T extends HTMLElement = HTMLDivElement>(
  active: boolean,
  onClose?: () => void,
) {
  const ref = useRef<T>(null);
  // Keep the latest onClose without re-running (and re-locking) the effect.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!active || !ref.current) return;

    const container = ref.current;
    // Remember the trigger so focus can return to it on close (WCAG 2.4.3).
    const previouslyFocused = document.activeElement as HTMLElement | null;

    const focusable = () =>
      container.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && onCloseRef.current) {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
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

    lockBodyScroll();
    container.addEventListener("keydown", handleKeyDown);

    return () => {
      container.removeEventListener("keydown", handleKeyDown);
      unlockBodyScroll();
      // Restore focus to the trigger, but only if focus is still inside the
      // (now closing) modal or has fallen to <body> — never steal focus the
      // app has deliberately moved elsewhere.
      if (previouslyFocused && typeof previouslyFocused.focus === "function") {
        const activeEl = document.activeElement;
        if (!activeEl || activeEl === document.body || container.contains(activeEl)) {
          previouslyFocused.focus();
        }
      }
    };
  }, [active]);

  return ref;
}
