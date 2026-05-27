import { useEffect, useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";

/**
 * Overflow dropdown for the top nav at the "compact" viewport tier
 * (720–1320 px). Holds NavLinks that would otherwise crowd out the
 * primary entry points (Dashboard / New Search). Click-outside + ESC
 * close. The trigger picks up the `.active` styling when any of its
 * children carry React Router's `.active` class, so a user currently
 * on a hidden route (e.g. `/api`) still sees a visual indicator of
 * "you are here, inside this menu".
 *
 * Stays out of the mobile drawer flow — the existing hamburger drawer
 * (<720 px) already lists every nav item, so we hide this component
 * entirely below that breakpoint via CSS in Layout.css.
 */
export function NavMoreDropdown({
  label,
  title,
  children,
}: {
  label: string;
  title?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [hasActiveChild, setHasActiveChild] = useState(false);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click + Escape.
  useEffect(() => {
    if (!open) return;
    const onClickAway = (event: MouseEvent) => {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClickAway);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClickAway);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Watch the rendered children for React Router's `.active` class so the
  // trigger highlights when the user is on a route inside the dropdown.
  // The class is only applied after navigation so a one-shot observer
  // would miss route changes — we use a small MutationObserver on the
  // panel subtree, which is cheap because the panel is tiny.
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const sync = () => {
      setHasActiveChild(Boolean(wrapper.querySelector(".layout__nav-item.active")));
    };
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(wrapper, {
      subtree: true,
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => observer.disconnect();
  });

  // Auto-close when the user picks an item so the page transition isn't
  // blocked by an open popover.
  const handlePanelClick = (event: React.MouseEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.closest("a,button")) setOpen(false);
  };

  return (
    <div className="layout__nav-more" ref={wrapperRef}>
      <button
        type="button"
        className={`layout__nav-item layout__nav-more-trigger${hasActiveChild ? " active" : ""}`}
        aria-haspopup="menu"
        aria-expanded={open}
        title={title}
        onClick={() => setOpen((value) => !value)}
      >
        <MoreHorizontal size={14} strokeWidth={1.5} /> {label}
        {hasActiveChild && <span className="layout__nav-more-dot" aria-hidden="true" />}
      </button>
      {open && (
        <div
          className="layout__nav-more-panel"
          role="menu"
          aria-label={label}
          onClick={handlePanelClick}
        >
          {children}
        </div>
      )}
    </div>
  );
}
