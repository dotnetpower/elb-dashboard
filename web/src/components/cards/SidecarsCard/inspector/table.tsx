/**
 * Sidecar HTTP request inspector — request table.
 *
 * Paginated, sticky-header table of captured requests with click /
 * keyboard row selection and a "show more" control. Pure presentation;
 * selection + pagination are owned by the parent `VariantA`.
 */

import { ChevronRight } from "lucide-react";
import type { MockReq } from "./types";
import { fmtBytes, fmtMs, fmtTime, latencyTone } from "./format";
import { DegradedPill, MethodPill, StatusPill, Td, Th } from "./atoms";

export function TableA({
  data,
  selectedId,
  onPick,
  limit,
  onShowMore,
}: {
  data: MockReq[];
  selectedId?: string;
  onPick: (r: MockReq) => void;
  limit: number;
  onShowMore: () => void;
}) {
  const visible = data.slice(0, limit);
  const remaining = Math.max(0, data.length - limit);
  return (
    <div
      style={{
        maxHeight: 320,
        overflowY: "auto",
        border: "1px solid var(--border-weak)",
        borderRadius: 6,
        position: "relative",
        zIndex: 0,
      }}
    >
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
        <thead
          style={{
            position: "sticky",
            top: 0,
            background: "rgba(0,0,0,0.6)",
            backdropFilter: "blur(8px)",
            zIndex: 1,
          }}
        >
          <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
            <Th>Time</Th>
            <Th>Method</Th>
            <Th>Path</Th>
            <Th>Caller</Th>
            <Th align="right">Status</Th>
            <Th align="right">Duration</Th>
            <Th align="right">Size</Th>
            <Th></Th>
          </tr>
        </thead>
        <tbody>
          {visible.length === 0 && (
            <tr>
              <td
                colSpan={8}
                style={{
                  padding: "18px 14px",
                  textAlign: "center",
                  color: "var(--text-muted)",
                  fontSize: 11,
                }}
              >
                No requests match the current filter.
              </td>
            </tr>
          )}
          {visible.map((d) => (
            <tr
              key={d.id}
              tabIndex={0}
              onClick={() => onPick(d)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onPick(d);
                }
              }}
              style={{
                cursor: "pointer",
                background: selectedId === d.id ? "rgba(122,167,255,0.08)" : undefined,
                borderTop: "1px solid var(--border-weak)",
                outline: "none",
              }}
            >
              <Td>{fmtTime(d.ts)}</Td>
              <Td>
                <MethodPill method={d.method} />
              </Td>
              <Td>
                <code style={{ fontSize: 11 }}>{d.path}</code>
              </Td>
              <Td>
                <span style={{ color: "var(--text-muted)" }}>
                  {d.caller.split("@")[0]}
                </span>
              </Td>
              <Td align="right">
                <span
                  style={{
                    display: "inline-flex",
                    justifyContent: "flex-end",
                    gap: 4,
                    flexWrap: "wrap",
                  }}
                >
                  <StatusPill code={d.status} />
                  {d.degraded && <DegradedPill />}
                </span>
              </Td>
              <Td align="right">
                <span
                  style={{
                    color: latencyTone(d.durationMs),
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {fmtMs(d.durationMs)}
                </span>
              </Td>
              <Td align="right">
                <span style={{ color: "var(--text-muted)" }}>
                  {fmtBytes(d.responseBytes)}
                </span>
              </Td>
              <Td align="right">
                <ChevronRight
                  size={11}
                  style={{ color: "var(--text-faint, var(--text-muted))" }}
                />
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
      {remaining > 0 && (
        <button
          type="button"
          onClick={onShowMore}
          style={{
            width: "100%",
            padding: "8px 10px",
            background: "rgba(255,255,255,0.04)",
            border: "none",
            borderTop: "1px solid var(--border-weak)",
            color: "var(--text-muted)",
            cursor: "pointer",
            fontSize: 11,
            fontFamily: "inherit",
          }}
        >
          Show {Math.min(50, remaining)} more · {remaining} hidden
        </button>
      )}
    </div>
  );
}
