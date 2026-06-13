/**
 * Sidecar HTTP request inspector (Variant A — the shipped design).
 *
 * Renders the live per-request detail panel on the `api` sidecar card:
 *   • a latency/status scatter chart ("is anything slow / failing now?")
 *   • a request table ("which URL / who / how long / what status?")
 *   • a drill-down ("what headers / body did that call carry?")
 *
 * Consumed by `HttpInspectorPanel.tsx`, which feeds it real captured
 * traffic (`InspectorRequest[]`) from `GET /api/monitor/sidecar-requests`.
 * The backend already masks `Authorization` / `Cookie` / `X-Api-Key` and
 * caps bodies — see `api/services/request_metrics.py`. This module is
 * pure presentation: it does not fetch.
 *
 * The presentation pieces are split into the sibling `inspector/` modules
 * (types, format helpers, atoms, code block, detail surfaces, scatter
 * chart, table). This file owns only `VariantA` — the stateful composition
 * that wires the time window, filters, and selection together.
 *
 * Provenance: extracted verbatim from the retired
 * `pages/mockups/SidecarInspectorMockups.tsx` design-exploration page
 * (issue #24). Only Variant A and its helpers are kept; the two
 * unshipped variants and the demo fixture were removed.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertOctagon, Pause, Play } from "lucide-react";

import type { MockReq } from "./inspector/types";
import { CountChips, Header, LiveIndicator, SearchBar } from "./inspector/atoms";
import { Drawer, InlineRequestDetail } from "./inspector/detail";
import { ScatterChart } from "./inspector/scatterChart";
import { TableA } from "./inspector/table";

export type { InspectorRequest } from "./inspector/types";

/* ==================================================================== */
/* VARIANT A — Timeline scatter + right-side drawer                     */
/* ==================================================================== */

export function VariantA({ data }: { data: MockReq[] }) {
  const [graphSelected, setGraphSelected] = useState<MockReq | null>(null);
  const [tableSelected, setTableSelected] = useState<MockReq | null>(null);
  const [paused, setPaused] = useState(false);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [query, setQuery] = useState("");
  const [tableLimit, setTableLimit] = useState(25);
  const tableDetailRef = useRef<HTMLDivElement | null>(null);

  // Esc closes drawer
  useEffect(() => {
    if (!graphSelected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setGraphSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [graphSelected]);

  // Time window filter — anchored to the most recent sample's timestamp
  // so live + fixture data both stay visible. (Mockup fixtures were
  // generated against NOW at module load; live data is recent by
  // definition.)
  const windowMin = 5;
  const referenceTs = data.length > 0 ? Math.max(...data.map((d) => d.ts)) : Date.now();
  const windowStart = referenceTs - windowMin * 60_000;
  const windowed = useMemo(
    () => data.filter((d) => d.ts >= windowStart),
    [data, windowStart],
  );

  const counts = useMemo(() => {
    const c = { ok: 0, redirect: 0, client: 0, server: 0, degraded: 0 };
    for (const d of windowed) {
      if (d.degraded) c.degraded++;
      if (d.status >= 500) c.server++;
      else if (d.status >= 400) c.client++;
      else if (d.status >= 300) c.redirect++;
      else c.ok++;
    }
    return c;
  }, [windowed]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return windowed.filter((d) => {
      if (errorsOnly && d.status < 400 && !d.degraded) return false;
      if (
        q &&
        !d.path.toLowerCase().includes(q) &&
        !d.caller.toLowerCase().includes(q) &&
        !d.requestId.toLowerCase().includes(q) &&
        !String(d.status).includes(q) &&
        !(d.degraded && "degraded".includes(q)) &&
        !(d.degradedReasons ?? []).some((reason) => reason.toLowerCase().includes(q))
      ) {
        return false;
      }
      return true;
    });
  }, [windowed, errorsOnly, query]);

  // Reset paginated cap when filter set changes
  useEffect(() => {
    setTableLimit(25);
    setTableSelected(null);
  }, [errorsOnly, query, windowMin]);

  useEffect(() => {
    if (!tableSelected) return;
    const frameId = window.requestAnimationFrame(() => {
      tableDetailRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
        inline: "nearest",
      });
      tableDetailRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [tableSelected]);

  return (
    <div
      className="glass-card"
      style={{ padding: 14, position: "relative", overflow: "hidden" }}
    >
      <Header
        eyebrow="API sidecar"
        title="HTTP requests"
        right={
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              flexWrap: "wrap",
            }}
          >
            <LiveIndicator paused={paused} />
            <CountChips counts={counts} />
            <button
              type="button"
              className="glass-button"
              onClick={() => setErrorsOnly((v) => !v)}
              aria-pressed={errorsOnly}
              title={errorsOnly ? "Show all requests" : "Show only 4xx / 5xx / degraded"}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                fontSize: 10,
                padding: "3px 7px",
                color: errorsOnly ? "var(--danger)" : undefined,
                borderColor: errorsOnly ? "var(--danger)" : undefined,
              }}
            >
              <AlertOctagon size={11} />
              Errors
            </button>
            <button
              type="button"
              className="glass-button"
              onClick={() => setPaused((p) => !p)}
              aria-pressed={paused}
              aria-label={paused ? "Resume live updates" : "Pause for review"}
              title={paused ? "Resume live updates" : "Pause for review"}
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 10,
                padding: 5,
                width: 26,
                height: 22,
              }}
            >
              {paused ? <Play size={12} /> : <Pause size={12} />}
            </button>
          </div>
        }
      />
      <div style={{ position: "relative" }}>
        <ScatterChart
          data={filtered}
          windowStart={windowStart}
          windowEnd={referenceTs}
          onPick={setGraphSelected}
          selectedId={graphSelected?.id}
        />
        {paused && (
          <div
            style={{
              position: "absolute",
              top: 8,
              left: 12,
              padding: "2px 8px",
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: "0.08em",
              color: "var(--warning)",
              border: "1px solid var(--warning)",
              borderRadius: 3,
              background: "rgba(240, 198, 116, 0.08)",
            }}
          >
            PAUSED
          </div>
        )}
      </div>
      <SearchBar
        value={query}
        onChange={setQuery}
        total={windowed.length}
        shown={filtered.length}
      />
      <TableA
        data={filtered}
        selectedId={tableSelected?.id}
        onPick={setTableSelected}
        limit={tableLimit}
        onShowMore={() => setTableLimit((n) => n + 50)}
      />
      {tableSelected && (
        <div
          ref={tableDetailRef}
          tabIndex={-1}
          style={{ scrollMarginTop: 12, outline: "none" }}
        >
          <InlineRequestDetail
            req={tableSelected}
            onClose={() => setTableSelected(null)}
          />
        </div>
      )}
      {graphSelected && (
        <Drawer onClose={() => setGraphSelected(null)} req={graphSelected} />
      )}
    </div>
  );
}
