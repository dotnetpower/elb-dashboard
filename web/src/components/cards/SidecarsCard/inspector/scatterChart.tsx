/**
 * Sidecar HTTP request inspector — latency/status scatter chart.
 *
 * Log-scale latency vs time SVG scatter with an SLA reference line,
 * hover crosshair + tooltip, and click-to-select. Pure presentation;
 * the time window and selection are owned by the parent `VariantA`.
 */

import { useRef, useState } from "react";
import type { MockReq } from "./types";
import {
  DEGRADED_COLOR,
  DEGRADED_RING,
  clamp,
  fmtMs,
  fmtTime,
  latencyTicks,
  latencyTone,
  niceLatencyCeil,
  niceLatencyFloor,
  requestTone,
  statusTone,
  trianglePoints,
  windowMinLabel,
} from "./format";
import { DegradedPill, LegendDot, MethodPill, StatusPill } from "./atoms";

export function ScatterChart({
  data,
  windowStart,
  windowEnd,
  onPick,
  selectedId,
}: {
  data: MockReq[];
  windowStart: number;
  windowEnd: number;
  onPick: (r: MockReq) => void;
  selectedId?: string;
}) {
  const W = 880;
  const H = 220;
  const PAD = { l: 58, r: 16, t: 16, b: 44 };
  const POINT_EDGE_GAP = 8;
  const innerW = W - PAD.l - PAD.r;
  const innerH = H - PAD.t - PAD.b;
  // Anchor the time axis to the explicit window so the SLA line and ticks
  // stay stable as the user filters/searches (the data subset shouldn't
  // squish all dots into the right edge).
  const minTs = windowStart;
  const maxTs = windowEnd;
  const tRange = maxTs - minTs || 60_000;
  const durations = data.map((item) => Math.max(1, item.durationMs));
  const observedMin = durations.length > 0 ? Math.min(...durations) : 5;
  const observedMax = durations.length > 0 ? Math.max(...durations) : 3000;
  const yDomainMin = niceLatencyFloor(Math.max(1, observedMin * 0.75));
  const yDomainMax = Math.max(
    niceLatencyCeil(Math.max(observedMax * 1.18, yDomainMin * 2)),
    yDomainMin * 2,
  );
  const yMax = Math.log10(yDomainMax);
  const yMin = Math.log10(yDomainMin);
  const xOf = (ts: number) => PAD.l + ((ts - minTs) / tRange) * innerW;
  const pointXOf = (ts: number) =>
    clamp(xOf(ts), PAD.l + POINT_EDGE_GAP, W - PAD.r - POINT_EDGE_GAP);
  const yOf = (ms: number) => {
    const lv = Math.log10(Math.max(yDomainMin, Math.min(yDomainMax, ms)));
    return PAD.t + (1 - (lv - yMin) / (yMax - yMin)) * innerH;
  };
  const pointYOf = (ms: number) =>
    clamp(yOf(ms), PAD.t + POINT_EDGE_GAP, H - PAD.b - POINT_EDGE_GAP);

  const yTicks = latencyTicks(yDomainMin, yDomainMax);
  const xTickCount = 6;
  const xTicks = Array.from(
    { length: xTickCount },
    (_, i) => minTs + (i / (xTickCount - 1)) * tRange,
  );

  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ r: MockReq; x: number; y: number } | null>(null);

  const positionFromEvent = (e: React.MouseEvent<SVGElement>, r: MockReq) => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    setHover({ r, x: e.clientX - rect.left, y: e.clientY - rect.top });
  };

  return (
    <div ref={wrapRef} style={{ marginTop: 10, marginBottom: 12, position: "relative" }}>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
        {/* brighter plot background panel */}
        <rect
          x={PAD.l}
          y={PAD.t}
          width={innerW}
          height={innerH}
          fill="rgba(255,255,255,0.07)"
          stroke="var(--border-weak)"
          strokeWidth={0.5}
          rx={6}
        />
        {/* y gridlines */}
        {yTicks.map((y) => (
          <line
            key={`yg-${y}`}
            x1={PAD.l}
            x2={W - PAD.r}
            y1={yOf(y)}
            y2={yOf(y)}
            stroke="rgba(255,255,255,0.07)"
            strokeWidth={0.5}
          />
        ))}
        {/* x gridlines */}
        {xTicks.map((t, i) => (
          <line
            key={`xg-${i}`}
            x1={xOf(t)}
            x2={xOf(t)}
            y1={PAD.t}
            y2={H - PAD.b}
            stroke="rgba(255,255,255,0.05)"
            strokeWidth={0.5}
          />
        ))}
        {/* SLA reference */}
        {yDomainMin <= 2000 && yDomainMax >= 2000 && (
          <>
            <line
              x1={PAD.l}
              x2={W - PAD.r}
              y1={yOf(2000)}
              y2={yOf(2000)}
              stroke="var(--danger)"
              strokeDasharray="4 3"
              strokeWidth={1}
              opacity={0.6}
            >
              <title>
                SLA target — requests above this line breach the 2 s p95 budget
              </title>
            </line>
            <text
              x={W - PAD.r - 6}
              y={yOf(2000) - 4}
              fill="var(--danger)"
              fontSize="9"
              textAnchor="end"
            >
              SLA 2000 ms
            </text>
          </>
        )}
        {/* y axis line */}
        <line
          x1={PAD.l}
          x2={PAD.l}
          y1={PAD.t}
          y2={H - PAD.b}
          stroke="var(--text-muted)"
          strokeWidth={1}
        />
        {/* x axis line */}
        <line
          x1={PAD.l}
          x2={W - PAD.r}
          y1={H - PAD.b}
          y2={H - PAD.b}
          stroke="var(--text-muted)"
          strokeWidth={1}
        />
        {/* y tick marks + labels */}
        {yTicks.map((y) => (
          <g key={`yt-${y}`}>
            <line
              x1={PAD.l - 4}
              x2={PAD.l}
              y1={yOf(y)}
              y2={yOf(y)}
              stroke="var(--text-muted)"
              strokeWidth={1}
            />
            <text
              x={PAD.l - 7}
              y={yOf(y) + 3}
              fill="var(--text-muted)"
              fontSize="9"
              textAnchor="end"
            >
              {y}
            </text>
          </g>
        ))}
        {/* x tick marks + labels */}
        {xTicks.map((t, i) => (
          <g key={`xt-${i}`}>
            <line
              x1={xOf(t)}
              x2={xOf(t)}
              y1={H - PAD.b}
              y2={H - PAD.b + 4}
              stroke="var(--text-muted)"
              strokeWidth={1}
            />
            <text
              x={xOf(t)}
              y={H - PAD.b + 14}
              fill="var(--text-muted)"
              fontSize="9"
              textAnchor="middle"
            >
              {fmtTime(t)}
            </text>
          </g>
        ))}
        {/* axis titles */}
        <text
          x={-(PAD.t + innerH / 2)}
          y={14}
          fill="var(--text-muted)"
          fontSize="10"
          textAnchor="middle"
          transform="rotate(-90)"
        >
          Latency (ms · log scale)
        </text>
        <text
          x={PAD.l + innerW / 2}
          y={H - 4}
          fill="var(--text-muted)"
          fontSize="10"
          textAnchor="middle"
        >
          Time (last {windowMinLabel(windowStart, windowEnd)})
        </text>
        {/* dots */}
        {data.length === 0 && (
          <text
            x={PAD.l + innerW / 2}
            y={PAD.t + innerH / 2}
            fill="var(--text-muted)"
            fontSize="11"
            textAnchor="middle"
          >
            No requests in selected window
          </text>
        )}
        {hover && (
          <g pointerEvents="none">
            <line
              x1={pointXOf(hover.r.ts)}
              x2={pointXOf(hover.r.ts)}
              y1={PAD.t}
              y2={H - PAD.b}
              stroke="rgba(255,255,255,0.18)"
              strokeWidth={0.6}
              strokeDasharray="2 3"
            />
            <line
              x1={PAD.l}
              x2={W - PAD.r}
              y1={pointYOf(hover.r.durationMs)}
              y2={pointYOf(hover.r.durationMs)}
              stroke="rgba(255,255,255,0.18)"
              strokeWidth={0.6}
              strokeDasharray="2 3"
            />
          </g>
        )}
        {data.map((d) => {
          const tone = requestTone(d);
          const isSelected = d.id === selectedId;
          const isHovered = hover?.r.id === d.id;
          const is5xx = d.status >= 500;
          const isDegraded = Boolean(d.degraded && d.status < 400);
          const baseR = is5xx ? 4 : 3;
          const r = isHovered || isSelected ? baseR + 2 : baseR;
          const cx = pointXOf(d.ts);
          const cy = pointYOf(d.durationMs);
          return (
            <g key={d.id}>
              {isSelected && !isDegraded && (
                <circle
                  cx={cx}
                  cy={cy}
                  r={r + 4}
                  fill="none"
                  stroke={tone.fg}
                  strokeWidth={1.6}
                  opacity={0.85}
                />
              )}
              {isSelected && isDegraded && (
                <polygon
                  points={trianglePoints(cx, cy, r + 7)}
                  fill="none"
                  stroke={tone.fg}
                  strokeWidth={1.6}
                  opacity={0.85}
                />
              )}
              {is5xx && (
                /* contrasting halo for server errors so they pop above 2xx noise */
                <circle
                  cx={cx}
                  cy={cy}
                  r={r + 2}
                  fill="none"
                  stroke={tone.fg}
                  strokeWidth={1}
                  opacity={0.5}
                />
              )}
              {isDegraded ? (
                <polygon
                  points={trianglePoints(cx, cy, r + 1)}
                  fill={tone.fg}
                  opacity={isHovered || isSelected ? 1 : 0.88}
                  stroke={isHovered ? "#ffffff" : DEGRADED_RING}
                  strokeWidth={isHovered ? 1 : 0.8}
                  style={{ cursor: "pointer" }}
                  onMouseEnter={(e) => positionFromEvent(e, d)}
                  onMouseMove={(e) => positionFromEvent(e, d)}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => onPick(d)}
                />
              ) : (
                <circle
                  cx={cx}
                  cy={cy}
                  r={r}
                  fill={tone.fg}
                  opacity={isHovered || isSelected ? 1 : 0.85}
                  stroke={isHovered ? "#ffffff" : "none"}
                  strokeWidth={isHovered ? 1 : 0}
                  style={{ cursor: "pointer" }}
                  onMouseEnter={(e) => positionFromEvent(e, d)}
                  onMouseMove={(e) => positionFromEvent(e, d)}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => onPick(d)}
                />
              )}
            </g>
          );
        })}
      </svg>
      {hover &&
        (() => {
          const wrap = wrapRef.current;
          const wrapW = wrap?.clientWidth ?? 800;
          const wrapH = wrap?.clientHeight ?? 240;
          const TIP_W = 260;
          const TIP_H = 124;
          // Flip horizontally if cursor is past the right midline (so tip
          // never sits on top of dot or clips off the right edge).
          const flipLeft = hover.x + 14 + TIP_W > wrapW;
          const left = flipLeft
            ? Math.max(hover.x - TIP_W - 14, 4)
            : Math.min(hover.x + 14, wrapW - TIP_W - 4);
          // Flip vertically if cursor is in the bottom half (so tip rises above).
          const flipUp = hover.y + TIP_H + 12 > wrapH;
          const top = flipUp
            ? Math.max(hover.y - TIP_H - 12, 4)
            : Math.min(hover.y + 14, wrapH - TIP_H - 4);
          return (
            <div
              role="tooltip"
              style={{
                position: "absolute",
                pointerEvents: "none",
                left,
                top,
                background: "rgba(10,14,24,0.94)",
                border: "1px solid var(--border-weak)",
                borderRadius: 8,
                padding: "8px 10px",
                fontSize: 11,
                color: "var(--text-primary)",
                boxShadow: "0 6px 22px rgba(0,0,0,0.45)",
                minWidth: 220,
                maxWidth: TIP_W,
                zIndex: 5,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginBottom: 4,
                }}
              >
                <MethodPill method={hover.r.method} />
                <StatusPill code={hover.r.status} />
                {hover.r.degraded && <DegradedPill />}
                <span
                  style={{
                    marginLeft: "auto",
                    color: latencyTone(hover.r.durationMs),
                    fontSize: 10,
                    fontWeight: 600,
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {fmtMs(hover.r.durationMs)}
                </span>
              </div>
              <div
                style={{
                  fontFamily: "var(--font-mono, monospace)",
                  fontSize: 11,
                  color: "var(--text-primary)",
                  wordBreak: "break-all",
                  marginBottom: 4,
                  lineHeight: 1.35,
                }}
              >
                {hover.r.path}
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 8,
                  fontSize: 10,
                  color: "var(--text-muted)",
                }}
              >
                <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                  {hover.r.caller}
                </span>
                <span>{fmtTime(hover.r.ts)}</span>
              </div>
              <div
                style={{
                  marginTop: 5,
                  fontSize: 10,
                  color: "var(--text-faint, var(--text-muted))",
                  borderTop: "1px solid var(--border-weak)",
                  paddingTop: 4,
                }}
              >
                Click point for full request / response
              </div>
            </div>
          );
        })()}
      <div
        style={{
          display: "flex",
          gap: 10,
          fontSize: 10,
          color: "var(--text-muted)",
          marginTop: 4,
        }}
      >
        <LegendDot color={statusTone(200).fg} label="2xx" />
        <LegendDot color={statusTone(304).fg} label="3xx" />
        <LegendDot color={statusTone(404).fg} label="4xx" />
        <LegendDot color={statusTone(500).fg} label="5xx" />
        <LegendDot color={DEGRADED_COLOR} label="degraded" shape="triangle" />
        <span style={{ marginLeft: "auto" }}>
          {data.length} samples · {fmtTime(windowStart)}–{fmtTime(windowEnd)}
        </span>
      </div>
    </div>
  );
}
