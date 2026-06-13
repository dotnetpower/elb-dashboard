/**
 * SidecarLogModal — expanded, readable log view for one Live Wall tile.
 *
 * The tile itself only renders the last ~6 lines, each truncated to a single
 * row, so a long message or a burst of errors can't be inspected in place.
 * This modal opens the full picture: it backfills a larger recent tail from
 * `GET /monitor/logs/{c}/recent`, merges it with the live SSE buffer the tile
 * already holds (so new lines keep arriving while it's open), wraps long
 * messages instead of clipping them, and lets the operator filter by level
 * (ALL / ERR / WARN) or free text. Clicking the tile's "N ERR" pill opens this
 * pre-filtered to errors, which answers "what are those 5 errors?".
 *
 * It is presentation-only: it consumes the live buffer passed in plus a
 * one-shot backfill fetch, and never opens its own SSE connection (the tile
 * owns that), so opening/closing the modal cannot multiply connections.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, X } from "lucide-react";

import {
  fetchRecentLogs,
  type LogLine,
  type SidecarContainer,
} from "@/api/sidecarLogs";

const BACKFILL_TAIL = 500;

type LevelFilter = "ALL" | "ERR" | "WARN";

interface Props {
  container: SidecarContainer;
  role: string;
  /** The tile's live SSE buffer — keeps the modal updating in real time. */
  liveLines: LogLine[];
  /** Pre-select a level filter (e.g. open straight to errors from the pill). */
  initialLevel?: LevelFilter;
  onClose: () => void;
}

/** Stable identity for a log line so backfill + live tail can be de-duped. */
function lineKey(l: LogLine): string {
  return `${l.ts}|${l.stream}|${l.text}`;
}

export function SidecarLogModal({ container, role, liveLines, initialLevel = "ALL", onClose }: Props) {
  const [backfill, setBackfill] = useState<LogLine[]>([]);
  const [backfillState, setBackfillState] = useState<"loading" | "ready" | "unavailable">("loading");
  const [level, setLevel] = useState<LevelFilter>(initialLevel);
  const [text, setText] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // One-shot backfill of older history. The live buffer only holds the last
  // ~60 lines, so this is what surfaces errors that already scrolled off.
  useEffect(() => {
    let cancelled = false;
    setBackfillState("loading");
    fetchRecentLogs(container, BACKFILL_TAIL)
      .then((lines) => {
        if (cancelled) return;
        setBackfill(lines);
        setBackfillState("ready");
      })
      .catch(() => {
        // 404 / 410 (backend without the recent route) or transient — fall
        // back to the live buffer alone rather than showing an error.
        if (!cancelled) setBackfillState("unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [container]);

  // Close on Escape (capture phase so it wins over any parent handler).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  // Merge backfill (older) + live (newer), de-dupe, sort oldest→newest.
  const merged = useMemo(() => {
    const byKey = new Map<string, LogLine>();
    for (const l of backfill) byKey.set(lineKey(l), l);
    for (const l of liveLines) byKey.set(lineKey(l), l);
    return Array.from(byKey.values()).sort((a, b) => {
      const ta = Date.parse(a.ts);
      const tb = Date.parse(b.ts);
      if (Number.isNaN(ta) || Number.isNaN(tb)) return 0;
      return ta - tb;
    });
  }, [backfill, liveLines]);

  const counts = useMemo(() => {
    let err = 0;
    let warn = 0;
    for (const l of merged) {
      if (l.level === "ERR") err += 1;
      else if (l.level === "WARN") warn += 1;
    }
    return { all: merged.length, err, warn };
  }, [merged]);

  const textRegex = useMemo(() => {
    if (!text.trim()) return null;
    try {
      return new RegExp(text, "i");
    } catch {
      return null;
    }
  }, [text]);

  const visible = useMemo(() => {
    return merged.filter((l) => {
      if (level === "ERR" && l.level !== "ERR") return false;
      if (level === "WARN" && l.level !== "WARN") return false;
      if (textRegex && !textRegex.test(l.text)) return false;
      return true;
    });
  }, [merged, level, textRegex]);

  // Keep the newest line in view as the live tail grows.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [visible.length]);

  const handleCopy = () => {
    const blob = visible
      .map((l) => `${l.ts} ${l.level ?? "INFO"} ${l.text}`)
      .join("\n");
    void navigator.clipboard.writeText(blob);
  };

  return createPortal(
    <div
      className="glass-dialog-backdrop"
      style={{ zIndex: 1100 }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`${container} logs`}
        className="glass-card sidecar-log-modal"
      >
        <header className="sidecar-log-modal__head">
          <div className="sidecar-log-modal__title">
            <span className="sidecar-log-modal__name">{container}</span>
            <span className="sidecar-log-modal__role">{role}</span>
          </div>
          <div className="sidecar-log-modal__head-actions">
            <button
              type="button"
              className="glass-button"
              onClick={handleCopy}
              disabled={visible.length === 0}
              title="Copy the visible lines"
              style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              <Copy size={13} strokeWidth={1.7} /> Copy
            </button>
            <button
              type="button"
              className="live-tile__icon-btn"
              onClick={onClose}
              title="Close"
              aria-label="Close logs"
            >
              <X size={14} strokeWidth={2} />
            </button>
          </div>
        </header>

        <div className="sidecar-log-modal__toolbar">
          <div className="sidecar-log-modal__levels" role="group" aria-label="Filter by level">
            <button
              type="button"
              className={`sidecar-log-modal__chip${level === "ALL" ? " is-active" : ""}`}
              onClick={() => setLevel("ALL")}
              aria-pressed={level === "ALL"}
            >
              All {counts.all}
            </button>
            <button
              type="button"
              className={`sidecar-log-modal__chip sidecar-log-modal__chip--err${level === "ERR" ? " is-active" : ""}`}
              onClick={() => setLevel("ERR")}
              aria-pressed={level === "ERR"}
              disabled={counts.err === 0}
            >
              ERR {counts.err}
            </button>
            <button
              type="button"
              className={`sidecar-log-modal__chip sidecar-log-modal__chip--warn${level === "WARN" ? " is-active" : ""}`}
              onClick={() => setLevel("WARN")}
              aria-pressed={level === "WARN"}
              disabled={counts.warn === 0}
            >
              WARN {counts.warn}
            </button>
          </div>
          <input
            type="text"
            className="live-wall__filter sidecar-log-modal__filter"
            placeholder='filter (regex, e.g. "timeout|job-218")'
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-label="Filter log lines"
          />
        </div>

        <div className="sidecar-log-modal__body" ref={scrollRef} role="log" aria-live="polite">
          {visible.length === 0 ? (
            <p className="sidecar-log-modal__empty">
              {backfillState === "loading"
                ? "loading recent logs…"
                : level !== "ALL" || textRegex
                  ? "no lines match the current filter"
                  : "no recent activity"}
            </p>
          ) : (
            visible.map((line, idx) => (
              <div key={`${lineKey(line)}-${idx}`} className="sidecar-log-modal__line">
                <span className="sidecar-log-modal__ts">{formatHms(line.ts)}</span>
                <span className={`live-tile__log-lvl live-tile__log-lvl--${line.level ?? "INFO"}`}>
                  {line.level ?? "INFO"}
                </span>
                <span className="sidecar-log-modal__msg">{line.text}</span>
              </div>
            ))
          )}
        </div>

        <footer className="sidecar-log-modal__foot">
          {backfillState === "unavailable" ? (
            <span>Showing the live tail only — recent history is not available from this deployment.</span>
          ) : (
            <span>
              Showing {visible.length} of {counts.all} lines · sanitised server-side (tokens, SAS,
              Authorization redacted).
            </span>
          )}
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function formatHms(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// Re-exported for the tile so it can type the level it opens with.
export type { LevelFilter };
