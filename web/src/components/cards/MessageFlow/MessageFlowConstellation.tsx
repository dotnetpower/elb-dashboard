/**
 * MessageFlowConstellation — the "Bounded Lanes (A1)" force-graph rendering of
 * the Service Bus message flow.
 *
 * Producers (left) → Broker (a bordered region with a Queue lane above a Topic
 * lane) → Consumers (right). Jobs are force-positioned by status: queued jobs
 * settle into the Queue lane, running jobs into the broker centre. Producers
 * are api-dominant (rounded-square glyph) with the occasional human user
 * (circle). Connection lines thin and fade with message age. Hovering a
 * submitter surfaces its session group (jobs sharing an alias) and dims the
 * rest. Clicking a job calls `onSelectBox` so the parent can open the JSON
 * detail modal.
 *
 * This is a pure presentation component over the live `MessageFlowSnapshot`.
 * It never fabricates data: there is no synthetic "session" entity on the
 * backend, so a session is simply the set of active jobs sharing a submitter
 * alias; when a field (query size, created_at) is missing the visual degrades
 * (minimum radius, neutral link age) instead of inventing a value. The empty
 * state is handled by the parent modal, not here.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { drag as d3drag } from "d3-drag";
import {
  forceCollide,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationNodeDatum,
} from "d3-force";
import { select } from "d3-selection";

import type { MessageFlowBox, MessageFlowSnapshot } from "@/api/messageFlow";

import { aliasTone } from "./colors";
import {
  ageStyle,
  bornMs,
  jobRadius,
  jobTooltip,
  producerKind,
  spread01,
} from "./constellationModel";

interface Props {
  snapshot: MessageFlowSnapshot;
  /** Click-through for a broker job (opens the JSON detail modal in the parent). */
  onSelectBox: (box: MessageFlowBox) => void;
  /** job_id of the currently open detail modal, for the selection ring. */
  selectedJobId?: string | null;
}

type NodeKind = "producer" | "cluster" | "job";

interface FlowNode extends SimulationNodeDatum {
  id: string;
  kind: NodeKind;
  alias: string;
  // producer
  pkind?: "api" | "user";
  count?: number;
  // cluster
  clusterName?: string;
  running?: number;
  queued?: number;
  // job
  box?: MessageFlowBox;
  r?: number;
  status?: string;
  born?: number | null;
  anchorX?: number;
  anchorY?: number;
}

interface FlowLink {
  id: string;
  source: FlowNode;
  target: FlowNode;
  alias: string;
  born: number | null;
}

export function MessageFlowConstellation({ snapshot, onSelectBox, selectedJobId }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const simRef = useRef<Simulation<FlowNode, undefined> | null>(null);
  // Preserve job positions across the 20s refetch so the layout does not jump.
  const posCacheRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  const hoveredRef = useRef<string | null>(null);
  // First mount gets a full settle; later rebuilds (20s refetch / resize) use a
  // gentle nudge so the graph does not visibly bounce while the operator reads.
  const firstBuildRef = useRef(true);
  const [size, setSize] = useState({ w: 0, h: 0 });

  // Subscriptions for the Topic lane come from the live Service Bus counts.
  const subscriptions = useMemo(
    () => (snapshot.sb_counts?.subscriptions ?? []).map((s) => s.name).filter(Boolean),
    [snapshot.sb_counts],
  );

  // Track container size (responsive width, fixed-ish height from CSS). Only
  // commit a new size when the rounded dimensions actually change, so a
  // ResizeObserver burst does not trigger a stream of full graph rebuilds.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (!r) return;
      const next = { w: Math.round(r.width), h: Math.round(r.height) };
      setSize((cur) => (cur.w === next.w && cur.h === next.h ? cur : next));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const svgEl = svgRef.current;
    const { w, h } = size;
    if (!svgEl || w < 40 || h < 40) return;

    const producers = snapshot.producers ?? [];
    const broker = snapshot.broker ?? [];
    const clusters = snapshot.consumers?.clusters ?? [];

    // ----- geometry: bordered broker region with Queue (top) / Topic (bottom)
    const bx0 = w * 0.3;
    const bx1 = w * 0.7;
    const by0 = 36;
    const by1 = h - 22;
    const bw = bx1 - bx0;
    const bh = by1 - by0;
    const cx = (bx0 + bx1) / 2;
    const cy = (by0 + by1) / 2;
    const mid = (by0 + by1) / 2;
    const prodX = w * 0.13;
    const clusX = w * 0.87;

    const svg = select(svgEl);
    svg.selectAll("*").remove();

    // ---------- static broker boundary + lane labels ----------
    const boundary = svg.append("g");
    boundary
      .append("rect")
      .attr("x", bx0)
      .attr("y", by0)
      .attr("width", bw)
      .attr("height", bh)
      .attr("rx", 14)
      .attr("fill", "rgba(255,255,255,0.01)")
      .attr("stroke", "var(--border-medium)")
      .attr("stroke-width", 1.1);
    boundary
      .append("line")
      .attr("x1", bx0 + 12)
      .attr("x2", bx1 - 12)
      .attr("y1", mid)
      .attr("y2", mid)
      .attr("stroke", "var(--border-weak)")
      .attr("stroke-dasharray", "4 6");

    const diamond = (x: number, y: number, r: number) =>
      `M${x},${y - r} L${x + r},${y} L${x},${y + r} L${x - r},${y} Z`;
    const labels = svg.append("g");
    labels
      .append("path")
      .attr("d", diamond(bx0 + 16, by0 + 14, 5))
      .attr("fill", "var(--bg-tertiary)")
      .attr("stroke", "var(--accent)")
      .attr("stroke-width", 1);
    labels
      .append("text")
      .attr("x", bx0 + 27)
      .attr("y", by0 + 17)
      .attr("font-size", 10)
      .attr("fill", "var(--text-muted)")
      .text(`Queue · ${snapshot.request_queue || "requests"}`);
    labels
      .append("rect")
      .attr("x", bx0 + 11)
      .attr("y", mid + 9)
      .attr("width", 10)
      .attr("height", 10)
      .attr("rx", 2)
      .attr("fill", "var(--bg-tertiary)")
      .attr("stroke", "var(--teal)")
      .attr("stroke-width", 1);
    labels
      .append("text")
      .attr("x", bx0 + 27)
      .attr("y", mid + 17)
      .attr("font-size", 10)
      .attr("fill", "var(--text-muted)")
      .text(`Topic · ${snapshot.completion_topic || "completions"}`);
    subscriptions.forEach((s, i) => {
      const gx = bx1 - 16;
      const gy = mid + 28 + i * 18;
      if (gy > by1 - 6) return;
      labels
        .append("circle")
        .attr("cx", gx)
        .attr("cy", gy)
        .attr("r", 3.5)
        .attr("fill", "var(--teal)")
        .attr("opacity", 0.8);
      labels
        .append("text")
        .attr("x", gx - 9)
        .attr("y", gy + 3)
        .attr("text-anchor", "end")
        .attr("font-size", 9)
        .attr("fill", "var(--text-faint)")
        .text(s);
    });

    // column captions
    [
      [prodX, "Producers"],
      [cx, "Broker"],
      [clusX, "Consumers"],
    ].forEach(([x, t]) => {
      svg
        .append("text")
        .attr("class", "mf-col-label")
        .attr("x", x as number)
        .attr("y", 18)
        .attr("text-anchor", "middle")
        .text(t as string);
    });

    // ---------- nodes ----------
    const cache = posCacheRef.current;
    const producerNodes: FlowNode[] = producers.map((p, i) => ({
      id: "p:" + p.alias,
      kind: "producer",
      alias: p.alias,
      pkind: producerKind(p.sources),
      count: p.job_count,
      fx: prodX,
      fy: 44 + ((i + 0.5) / Math.max(1, producers.length)) * (h - 70),
    }));
    const clusterNodes: FlowNode[] = clusters.map((c, i) => ({
      id: "c:" + (c.cluster_name || "unassigned"),
      kind: "cluster",
      alias: "\x00cluster",
      clusterName: c.cluster_name,
      running: c.running,
      queued: c.queued,
      fx: clusX,
      fy: 44 + ((i + 0.5) / Math.max(1, clusters.length)) * (h - 70),
    }));
    const jobNodes: FlowNode[] = broker.map((b) => {
      const cached = cache.get(b.job_id);
      return {
        id: b.job_id,
        kind: "job",
        alias: b.alias,
        box: b,
        status: b.status,
        born: bornMs(b.created_at),
        r: jobRadius(b.query_size),
        x: cached?.x ?? cx + spread01(b.job_id) * bw * 0.6,
        y: cached?.y ?? cy + spread01(b.job_id + "y") * bh * 0.6,
      };
    });

    const pById = new Map(producerNodes.map((d) => [d.alias, d]));
    const cByName = new Map(clusterNodes.map((d) => [d.clusterName || "", d]));
    const nodes = [...producerNodes, ...clusterNodes, ...jobNodes];

    const links: FlowLink[] = [];
    jobNodes.forEach((j) => {
      const p = pById.get(j.alias);
      if (p) links.push({ id: "l1:" + j.id, source: p, target: j, alias: j.alias, born: j.born ?? null });
      if (j.status !== "queued") {
        const c = cByName.get(j.box?.cluster_name || "");
        if (c) links.push({ id: "l2:" + j.id, source: j, target: c, alias: j.alias, born: j.born ?? null });
      }
    });

    // ---------- layers ----------
    const linkLayer = svg.append("g").attr("fill", "none").attr("stroke-linecap", "round");
    const sessionLayer = svg.append("g");
    const nodeLayer = svg.append("g");

    const linkSel = linkLayer
      .selectAll<SVGPathElement, FlowLink>("path")
      .data(links, (d) => d.id)
      .join("path")
      .attr("stroke", (d) => aliasTone(d.alias).accent);

    // Sessions = jobs sharing a submitter alias. Faint at rest, revealed on hover.
    const sessionAliases = Array.from(new Set(jobNodes.map((j) => j.alias)));
    const sessionG = sessionLayer
      .selectAll<SVGGElement, string>("g.mf-session")
      .data(sessionAliases, (d) => d)
      .join((enter) => {
        const g = enter.append("g").attr("class", "mf-session");
        g.append("rect")
          .attr("rx", 7)
          .attr("fill", "none")
          .attr("stroke-dasharray", "4 5")
          .attr("stroke-width", 1);
        g.append("text").attr("class", "mf-session-label").attr("font-size", 9);
        return g;
      });
    sessionG.select("rect").attr("stroke", (a) => aliasTone(a).accent).attr("stroke-opacity", 0.1);
    sessionG
      .select("text")
      .attr("fill", "var(--text-faint)")
      .attr("opacity", 0)
      .text((a) => `${a} · ${jobNodes.filter((j) => j.alias === a).length}`);

    const nodeSel = nodeLayer
      .selectAll<SVGGElement, FlowNode>("g.mf-node")
      .data(nodes, (d) => d.id)
      .join((enter) => {
        const g = enter.append("g").attr("class", "mf-node");

        // producers
        const prod = g.filter((d) => d.kind === "producer");
        prod.each(function (d) {
          const sel = select(this);
          const tone = aliasTone(d.alias);
          if (d.pkind === "api") {
            sel
              .append("rect")
              .attr("x", -9)
              .attr("y", -9)
              .attr("width", 18)
              .attr("height", 18)
              .attr("rx", 5)
              .attr("fill", "var(--bg-tertiary)")
              .attr("stroke", tone.accent)
              .attr("stroke-width", 1.3);
            sel
              .append("path")
              .attr("d", "M-4,-3 H4 M-4,0 H4 M-4,3 H4")
              .attr("stroke", tone.accent)
              .attr("stroke-width", 1)
              .attr("opacity", 0.75);
          } else {
            sel
              .append("circle")
              .attr("r", 7)
              .attr("fill", tone.fill)
              .attr("stroke", tone.accent)
              .attr("stroke-width", 1.3);
          }
          sel
            .append("text")
            .attr("x", -14)
            .attr("dy", "0.32em")
            .attr("text-anchor", "end")
            .attr("font-size", 11)
            .attr("fill", "var(--text-primary)")
            .text(`${d.alias}${d.pkind === "api" ? " ·api" : ""} (${d.count})`);
        });

        // clusters
        const clus = g.filter((d) => d.kind === "cluster");
        clus
          .append("rect")
          .attr("x", -8)
          .attr("y", -16)
          .attr("width", 16)
          .attr("height", 32)
          .attr("rx", 8)
          .attr("fill", "var(--bg-tertiary)")
          .attr("stroke", "var(--border-medium)");
        clus
          .append("text")
          .attr("x", 14)
          .attr("dy", "-0.1em")
          .attr("font-size", 11)
          .attr("fill", "var(--text-primary)")
          .text((d) => d.clusterName || "unassigned");
        clus
          .append("text")
          .attr("x", 14)
          .attr("dy", "1.15em")
          .attr("font-size", 9)
          .attr("fill", "var(--text-faint)")
          .text((d) => `● ${d.running} · ○ ${d.queued}`);

        // jobs
        const job = g.filter((d) => d.kind === "job");
        job
          .append("circle")
          .attr("class", "mf-job-core")
          .attr("r", (d) => d.r ?? 4)
          .attr("fill", (d) => (d.status === "queued" ? "transparent" : aliasTone(d.alias).fill))
          .attr("stroke", (d) => aliasTone(d.alias).accent)
          .attr("stroke-width", 1.2)
          .attr("stroke-dasharray", (d) => (d.status === "queued" ? "3 3" : null));
        job
          .filter((d) => d.status === "running")
          .append("circle")
          .attr("r", (d) => (d.r ?? 4) + 2.5)
          .attr("fill", "none")
          .attr("stroke", (d) => aliasTone(d.alias).accent)
          .attr("stroke-opacity", 0.18);
        job.append("title").text((d) => (d.box ? jobTooltip(d.box) : ""));

        return g;
      });

    nodeSel
      .style("cursor", (d) => (d.kind === "job" ? "pointer" : "default"))
      .on("mouseenter", (_e, d) => {
        hoveredRef.current = d.alias;
        applyHover();
      })
      .on("mouseleave", () => {
        hoveredRef.current = null;
        applyHover();
      })
      .on("click", (_e, d) => {
        if (d.kind === "job" && d.box) onSelectBox(d.box);
      });

    // Keyboard accessibility: job nodes are focusable buttons so a keyboard
    // user can tab to a job and press Enter/Space to open its JSON detail —
    // parity with the previous lane UI's <button> tiles. Focus also lights up
    // the submitter chain (mirrors hover) so the focused node is obvious.
    const jobNodeSel = nodeSel.filter((d) => d.kind === "job");
    jobNodeSel
      .attr("tabindex", 0)
      .attr("role", "button")
      .attr("aria-label", (d) =>
        d.box
          ? `View JSON for ${d.box.program ?? "blast"} job ${d.box.job_id} (${d.box.status})`
          : null,
      )
      .on("focus", (_e, d) => {
        hoveredRef.current = d.alias;
        applyHover();
      })
      .on("blur", () => {
        hoveredRef.current = null;
        applyHover();
      })
      .on("keydown", (e, d) => {
        if ((e.key === "Enter" || e.key === " ") && d.box) {
          e.preventDefault();
          onSelectBox(d.box);
        }
      });

    function applyHover() {
      const hv = hoveredRef.current;
      nodeSel.style("opacity", (d) =>
        hv == null || d.alias === hv || d.kind === "cluster" ? 1 : 0.16,
      );
      linkSel.attr("opacity", (d) => (hv == null ? 1 : d.alias === hv ? 1 : 0.06));
      sessionG.each(function (a) {
        const on = hv != null && a === hv;
        const g = select(this);
        g.select("rect").attr("stroke-opacity", on ? 0.5 : 0.1);
        g.select(".mf-session-label").attr("opacity", on ? 1 : 0);
        g.style("opacity", hv == null || on ? 1 : 0.5);
      });
    }

    // ---------- simulation ----------
    simRef.current?.stop();
    const sim = forceSimulation<FlowNode>(nodes)
      .force("collide", forceCollide<FlowNode>((d) => (d.kind === "job" ? (d.r ?? 4) + 3 : 15)))
      .force(
        "x",
        forceX<FlowNode>((d) => {
          if (d.kind !== "job") return d.fx ?? d.x ?? cx;
          if (d.status === "queued") return bx0 + bw * 0.32 + spread01(d.id) * bw * 0.22;
          return cx + spread01(d.id) * bw * 0.5;
        }).strength((d) => (d.kind === "job" ? 0.05 : 0)),
      )
      .force(
        "y",
        forceY<FlowNode>((d) => {
          if (d.kind !== "job") return d.fy ?? d.y ?? cy;
          if (d.status === "queued") return by0 + bh * 0.27 + spread01(d.id + "y") * bh * 0.12;
          return mid + spread01(d.id + "y") * bh * 0.16;
        }).strength((d) => (d.kind === "job" ? 0.06 : 0)),
      )
      .on("tick", ticked);
    // First mount settles fully; subsequent rebuilds (refetch / resize) reuse
    // the cached positions and only need a gentle nudge, so the still image
    // stays calm instead of bouncing every 20 seconds.
    const firstBuild = firstBuildRef.current;
    firstBuildRef.current = false;
    sim
      .alpha(firstBuild ? 0.9 : 0.25)
      .alphaDecay(firstBuild ? 0.045 : 0.12)
      .alphaTarget(0)
      .restart();
    simRef.current = sim;

    const dragBehavior = d3drag<SVGGElement, FlowNode>()
      .on("start", (_event, d) => {
        sim.alphaTarget(0.12).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on("drag", (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on("end", (_event, d) => {
        sim.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });
    nodeSel.filter((d) => d.kind === "job").call(dragBehavior);

    function ticked() {
      const now = Date.now();
      jobNodes.forEach((d) => {
        d.x = Math.max(bx0 + 6, Math.min(bx1 - 6, d.x ?? cx));
        d.y = Math.max(by0 + 6, Math.min(by1 - 6, d.y ?? cy));
        cache.set(d.id, { x: d.x, y: d.y });
      });
      linkSel
        .attr("d", (l) => {
          const sx = l.source.x ?? 0;
          const sy = l.source.y ?? 0;
          const tx = l.target.x ?? 0;
          const ty = l.target.y ?? 0;
          const mx = (sx + tx) / 2;
          return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}`;
        })
        .attr("stroke-width", (l) => ageStyle(l.born, now).w)
        .attr("stroke-opacity", (l) => {
          const s = ageStyle(l.born, now);
          const hv = hoveredRef.current;
          return hv && l.alias !== hv ? s.op * 0.18 : s.op;
        });
      nodeSel.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
      sessionG.each(function (a) {
        const members = jobNodes.filter((j) => j.alias === a);
        const node = select(this);
        if (!members.length) {
          node.style("display", "none");
          return;
        }
        node.style("display", null);
        let x0 = Infinity;
        let y0 = Infinity;
        let x1 = -Infinity;
        let y1 = -Infinity;
        members.forEach((m) => {
          const r = m.r ?? 4;
          x0 = Math.min(x0, (m.x ?? 0) - r);
          y0 = Math.min(y0, (m.y ?? 0) - r);
          x1 = Math.max(x1, (m.x ?? 0) + r);
          y1 = Math.max(y1, (m.y ?? 0) + r);
        });
        const pad = 7;
        node
          .select("rect")
          .attr("x", x0 - pad)
          .attr("y", y0 - pad)
          .attr("width", x1 - x0 + pad * 2)
          .attr("height", y1 - y0 + pad * 2);
        node
          .select("text")
          .attr("x", x0 - pad + 2)
          .attr("y", y0 - pad - 3);
      });
    }

    // Prune stale cache entries (jobs that completed/left the snapshot).
    const liveIds = new Set(jobNodes.map((j) => j.id));
    for (const id of Array.from(cache.keys())) if (!liveIds.has(id)) cache.delete(id);

    return () => {
      sim.stop();
    };
    // `selectedJobId` is intentionally NOT a dependency: it only changes the
    // selection ring, handled by the dedicated effect below. Including it here
    // would rebuild the whole SVG and restart the simulation on every job
    // click — the jarring re-settle this component is meant to avoid.
  }, [snapshot, size, subscriptions, onSelectBox]);

  // Selection ring: update just the selected job's stroke without rebuilding
  // the graph. Runs after the build effect (declaration order) so it re-applies
  // the ring after a refetch/resize rebuild too.
  useEffect(() => {
    const svgEl = svgRef.current;
    if (!svgEl) return;
    select(svgEl)
      .selectAll<SVGCircleElement, FlowNode>("circle.mf-job-core")
      .attr("stroke-width", (d) => (d && d.id === selectedJobId ? 2.4 : 1.2));
  }, [selectedJobId, size, snapshot]);

  return (
    <div ref={containerRef} className="mf-constellation">
      <svg ref={svgRef} role="img" aria-label="Service Bus message flow constellation" />
    </div>
  );
}
