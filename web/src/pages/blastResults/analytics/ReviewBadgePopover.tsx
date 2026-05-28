import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

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
  const wrapRef = useRef<HTMLSpanElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  // Close on outside click. Hover open/close is handled by JSX events;
  // this only protects against click-opened popovers that the user
  // dismisses by clicking elsewhere.
  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (wrapRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  // Flip above the cell if the popover would overflow the viewport
  // (rows near the bottom of the table on short screens).
  useLayoutEffect(() => {
    if (!open) return;
    const popEl = popRef.current;
    const wrapEl = wrapRef.current;
    if (!popEl || !wrapEl) return;
    const wrapRect = wrapEl.getBoundingClientRect();
    const popHeight = popEl.offsetHeight;
    const margin = 12;
    const spaceBelow = window.innerHeight - wrapRect.bottom;
    if (spaceBelow < popHeight + margin && wrapRect.top > popHeight + margin) {
      setPlacement("top");
    } else {
      setPlacement("bottom");
    }
  }, [open]);

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
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
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
        onFocus={() => setOpen(true)}
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
      {open && (
        <div
          ref={popRef}
          id={popoverId}
          role="dialog"
          aria-label={`${tier.label} hit classification details`}
          className={`review-popover review-popover--${placement}`}
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
        </div>
      )}
    </span>
  );
}
