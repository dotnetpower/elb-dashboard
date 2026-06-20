import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";

import type { BlastHit } from "@/api/endpoints";

import { formatEvalue, formatPercent } from "./helpers";
import {
  REVIEW_TIERS,
  REVIEW_TIER_BY_KEY,
  missingClassificationFields,
  type ReviewStatus,
} from "./reviewBadgeMeta";

interface Props {
  hit: BlastHit;
}

/**
 * Pill-shaped Review badge that doubles as a popover trigger. The
 * popover reveals the classification rule the backend used (mirrored
 * in `reviewBadgeMeta.ts`) plus the current row's actual values, so a
 * researcher can see at a glance why a hit landed in this tier.
 *
 * Opens on hover OR focus OR click; closes on Esc, blur to outside,
 * or a second click. Keyboard accessible via a real `<button>`.
 */
export function ReviewBadgePopover({ hit }: Props) {
  const status: ReviewStatus = hit.review_status ?? "unclassified";
  const tier = REVIEW_TIER_BY_KEY[status];
  const popoverId = useId();

  const [open, setOpen] = useState(false);
  const [placement, setPlacement] = useState<"bottom" | "top">("bottom");
  // The popover is rendered in a portal to <body> with fixed positioning so it
  // escapes the results table's `overflow:hidden` card + `overflow-x:auto`
  // scroller (which previously clipped it) and the sticky table header (which
  // painted over it). `coords` is the viewport-relative anchor.
  const [coords, setCoords] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const wrapRef = useRef<HTMLSpanElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | undefined>(undefined);

  const cancelClose = useCallback(() => {
    if (closeTimer.current !== undefined) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = undefined;
    }
  }, []);

  // Hover bridge: the portaled popover is not a DOM child of the badge, so
  // moving the pointer from the badge onto the popover would otherwise fire the
  // badge's mouseleave and close it. A short close delay that either side can
  // cancel keeps it open while the pointer travels the gap.
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 120);
  }, [cancelClose]);

  const openNow = useCallback(() => {
    cancelClose();
    setOpen(true);
  }, [cancelClose]);

  useEffect(() => cancelClose, [cancelClose]);

  // Close on outside click. Hover open/close is handled by JSX events;
  // this only protects against click-opened popovers that the user
  // dismisses by clicking elsewhere.
  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (wrapRef.current?.contains(target)) return;
      if (popRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  // Compute the popover's fixed viewport position from the trigger rect, and
  // flip above the badge when there is not enough room below (rows near the
  // bottom of the viewport). Recomputed on scroll/resize while open so the
  // portaled popover tracks the badge.
  const reposition = useCallback(() => {
    const wrapEl = wrapRef.current;
    const popEl = popRef.current;
    if (!wrapEl || !popEl) return;
    const rect = wrapEl.getBoundingClientRect();
    const popHeight = popEl.offsetHeight;
    const popWidth = popEl.offsetWidth || 380;
    const margin = 12;
    const spaceBelow = window.innerHeight - rect.bottom;
    const place: "bottom" | "top" =
      spaceBelow < popHeight + margin && rect.top > popHeight + margin ? "top" : "bottom";
    const top = place === "top" ? rect.top - popHeight - 8 : rect.bottom + 8;
    let left = rect.left;
    left = Math.min(left, window.innerWidth - popWidth - margin);
    left = Math.max(margin, left);
    setPlacement(place);
    setCoords({ top, left });
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    reposition();
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open, reposition]);

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Escape" && open) {
      event.stopPropagation();
      setOpen(false);
    }
  };

  const missingFields =
    status === "unclassified" ? missingClassificationFields(hit) : [];

  return (
    <span
      ref={wrapRef}
      className="review-badge-wrap"
      onMouseEnter={openNow}
      onMouseLeave={scheduleClose}
    >
      <button
        type="button"
        className="review-badge"
        style={{
          borderColor: tier.color,
          color: tier.color,
        }}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-describedby={open ? popoverId : undefined}
        title={tier.reason}
        onFocus={openNow}
        onBlur={(event) => {
          // Keep open if focus moved into the popover content; close
          // otherwise. relatedTarget is null on programmatic blurs.
          const next = event.relatedTarget as Node | null;
          if (next && wrapRef.current?.contains(next)) return;
          setOpen(false);
        }}
        onClick={() => setOpen((prev) => !prev)}
        onKeyDown={onKeyDown}
      >
        {tier.label}
      </button>
      {open &&
        createPortal(
        <div
          ref={popRef}
          id={popoverId}
          role="dialog"
          aria-label={`${tier.label} hit classification details`}
          className={`review-popover review-popover--${placement}`}
          style={{
            position: "fixed",
            top: coords.top,
            left: coords.left,
            right: "auto",
            bottom: "auto",
          }}
          onMouseEnter={cancelClose}
          onMouseLeave={scheduleClose}
        >
          <div className="review-popover__header">
            <span className="review-popover__badge" style={{ color: tier.color, borderColor: tier.color }}>
              {tier.label}
            </span>
            <span className="review-popover__reason">{hit.review_reason ?? tier.reason}</span>
          </div>

          <div className="review-popover__hit">
            <div className="review-popover__hit-cell">
              <span className="review-popover__hit-label">% identity</span>
              <span className="review-popover__hit-value">{formatPercent(hit.pident)}</span>
            </div>
            <div className="review-popover__hit-cell">
              <span className="review-popover__hit-label">HSP cover</span>
              <span className="review-popover__hit-value">{formatPercent(hit.qcovs)}</span>
            </div>
            <div className="review-popover__hit-cell">
              <span className="review-popover__hit-label">E-value</span>
              <span className="review-popover__hit-value">{formatEvalue(hit.evalue)}</span>
            </div>
          </div>

          {status === "unclassified" && missingFields.length > 0 && (
            <div className="review-popover__note">
              Missing required field(s): {missingFields.join(", ")}.
            </div>
          )}

          <table className="rp-table">
            <thead>
              <tr>
                <th>Tier</th>
                <th>% identity</th>
                <th>HSP cover</th>
                <th>E-value</th>
              </tr>
            </thead>
            <tbody>
              {REVIEW_TIERS.map((row) => {
                const active = row.key === status;
                return (
                  <tr
                    key={row.key}
                    className={active ? "rp-table__row rp-table__row--active" : "rp-table__row"}
                  >
                    <td>
                      <span style={{ color: row.color, fontWeight: 600 }}>{row.label}</span>
                    </td>
                    <td>{row.thresholds.pident ?? "—"}</td>
                    <td>{row.thresholds.qcovs ?? "—"}</td>
                    <td>{row.thresholds.evalue ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <div className="review-popover__footnote">
            First tier whose conditions are all met wins. Thresholds mirror the
            backend classifier in <code>annotate_result_hit()</code>.
          </div>
        </div>,
        document.body,
      )}
    </span>
  );
}
