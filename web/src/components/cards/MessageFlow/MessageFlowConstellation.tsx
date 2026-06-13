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

import { aliasTone, isErrorStatus, jobTone } from "./colors";
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
}

interface FlowLink {
  id: string;
  source: FlowNode;
  target: FlowNode;
  alias: string;
  born: number | null;
  /** Job status driving link styling (running links stay bright regardless of age). */
  status: string;
  /** "active" while in flight, "settling" while a terminal job fades out. */
  lifecycle: "active" | "settling";
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
  // Bumped when the OS "reduce motion" preference flips, so the graph re-builds
  // and immediately honours the new setting without waiting for a remount.
  const [motionPrefTick, setMotionPrefTick] = useState(0);

  // Subscriptions for the Topic lane come from the live Service Bus counts.
  const subscriptions = useMemo(
    () => (snapshot.sb_counts?.subscriptions ?? []).map((s) => s.name).filter(Boolean),
    [snapshot.sb_counts],
  );

  // React to a live change of the prefers-reduced-motion media query.
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return;
    const onChange = () => setMotionPrefTick((n) => n + 1);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);

  // Track container size (responsive width, fixed-ish height from CSS). Only
  // commit a new size when the rounded dimensions actually change, and debounce
  // through requestAnimationFrame so a drag-resize burst coalesces into a
  // single rebuild instead of restarting the simulation on every pixel.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let raf = 0;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (!r) return;
      const next = { w: Math.round(r.width), h: Math.round(r.height) };
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        setSize((cur) => (cur.w === next.w && cur.h === next.h ? cur : next));
      });
    });
    ro.observe(el);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
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
    // A concise summary for screen readers (the per-node detail lives on the
    // focusable job buttons and the producer/cluster titles).
    svg.attr(
      "aria-label",
      `Service Bus message flow: ${producers.length} producer${
        producers.length === 1 ? "" : "s"
      }, ${broker.length} active job${broker.length === 1 ? "" : "s"}, ${
        clusters.length
      } consumer cluster${clusters.length === 1 ? "" : "s"}`,
    );

    // ---------- static broker boundary + lane labels ----------
    // Decorative chrome — hidden from assistive tech so the only exposed nodes
    // are the interactive job buttons (added later with role=button).
    const boundary = svg.append("g").attr("aria-hidden", "true");
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
    // Submitter aliases are UPNs (can be long) and cluster names can be long
    // too; clip them so a wide label does not run off the SVG edge.
    const truncate = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
    const labels = svg.append("g").attr("aria-hidden", "true");
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
      const status = (j.status ?? "").toLowerCase();
      const lifecycle = j.box?.lifecycle === "settling" ? "settling" : "active";
      const p = pById.get(j.alias);
      if (p)
        links.push({
          id: "l1:" + j.id,
          source: p,
          target: j,
          alias: j.alias,
          born: j.born ?? null,
          status,
          lifecycle,
        });
      // Job → cluster link only once the job has left the waiting lanes
      // (queued/pending are not yet placed on a cluster). Running/reducing and
      // recently-terminal settling jobs keep the link so the fade-out reads.
      const waiting = status === "queued" || status === "pending";
      if (!waiting) {
        const c = cByName.get(j.box?.cluster_name || "");
        if (c)
          links.push({
            id: "l2:" + j.id,
            source: j,
            target: c,
            alias: j.alias,
            born: j.born ?? null,
            status,
            lifecycle,
          });
      }
    });

    // Pre-group job nodes by submitter alias once, so the per-tick session
    // bounding-box pass is O(jobs) instead of O(sessions × jobs).
    const membersByAlias = new Map<string, FlowNode[]>();
    jobNodes.forEach((j) => {
      const list = membersByAlias.get(j.alias);
      if (list) list.push(j);
      else membersByAlias.set(j.alias, [j]);
    });

    // ---------- layers ----------
    // Links and session hulls are decorative; only the node layer carries the
    // focusable job buttons, so hide the rest from assistive tech.
    const linkLayer = svg
      .append("g")
      .attr("fill", "none")
      .attr("stroke-linecap", "round")
      .attr("aria-hidden", "true");
    // Particles ride above the links but below the nodes, so the "energy"
    // travelling producer → job → cluster reads clearly without obscuring the
    // interactive job glyphs.
    const particleLayer = svg
      .append("g")
      .attr("class", "mf-particles")
      .attr("aria-hidden", "true");
    const sessionLayer = svg.append("g").attr("aria-hidden", "true");
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
      .text((a) => `${truncate(a, 18)} · ${membersByAlias.get(a)?.length ?? 0}`);

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
            .text(`${truncate(d.alias, 22)}${d.pkind === "api" ? " ·api" : ""} (${d.count})`);
          sel
            .append("title")
            .text(`${d.alias} (${d.pkind}) — ${d.count} active job${d.count === 1 ? "" : "s"}`);
          sel
            .attr("role", "img")
            .attr(
              "aria-label",
              `${d.pkind} producer ${d.alias}, ${d.count} active job${d.count === 1 ? "" : "s"}`,
            );
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
          .text((d) => truncate(d.clusterName || "unassigned", 18));
        clus.append("title").text((d) => d.clusterName || "unassigned");
        clus.attr("role", "img").attr(
          "aria-label",
          (d) => `consumer cluster ${d.clusterName || "unassigned"}, ${d.running} running, ${d.queued} queued`,
        );
        clus
          .append("text")
          .attr("x", 14)
          .attr("dy", "1.15em")
          .attr("font-size", 9)
          .attr("fill", "var(--text-faint)")
          .text((d) => `● ${d.running} · ○ ${d.queued}`);

        // jobs — status-aware visuals. Terminal states (failed/cancelled/
        // completed) override the submitter colour so a finished or failed job
        // reads unambiguously; in-flight states keep the submitter's tone.
        const job = g.filter((d) => d.kind === "job");
        job.each(function (d) {
          const sel = select(this);
          const status = (d.status ?? "").toLowerCase();
          const tone = jobTone(status, d.alias);
          const waiting = status === "queued" || status === "pending";
          const settling = d.box?.lifecycle === "settling";
          const r = d.r ?? 4;

          sel
            .append("circle")
            .attr("class", "mf-job-core")
            .attr("r", r)
            .attr("fill", waiting ? "transparent" : tone.fill)
            .attr("stroke", tone.accent)
            .attr("stroke-width", 1.2)
            .attr(
              "stroke-dasharray",
              waiting ? "3 3" : status === "cancelled" ? "1 3" : null,
            );

          // Running/reducing get a soft halo; the CSS pulse animates it (and is
          // disabled under prefers-reduced-motion). Reducing pulses a touch
          // slower to read as "merging results" rather than "scanning".
          if (status === "running" || status === "reducing") {
            sel
              .append("circle")
              .attr(
                "class",
                status === "reducing" ? "mf-halo mf-halo--reducing" : "mf-halo",
              )
              .attr("r", r + 2.5)
              .attr("fill", "none")
              .attr("stroke", tone.accent)
              .attr("stroke-opacity", 0.2);
          }

          // Failed jobs carry a small broken-cross marker on top of the danger
          // tone so colour is never the only error signal (WCAG).
          if (isErrorStatus(status)) {
            sel
              .append("path")
              .attr("d", `M${-r * 0.6},${-r * 0.6} L${r * 0.6},${r * 0.6} M${r * 0.6},${-r * 0.6} L${-r * 0.6},${r * 0.6}`)
              .attr("stroke", "rgba(255,255,255,0.9)")
              .attr("stroke-width", 1.1)
              .attr("stroke-linecap", "round");
          }

          if (settling) sel.classed("mf-node--settling", true);
        });
        job.append("title").text((d) => (d.box ? jobTooltip(d.box) : ""));

        return g;
      });

    nodeSel
      .style("cursor", (d) => (d.kind === "job" ? "pointer" : "default"))
      .on("mouseenter", (_e, d) => {
        // Only producers and jobs carry a meaningful submitter alias. Cluster
        // nodes share a sentinel alias, so hovering one must NOT set it as the
        // active alias (that would dim every other node in the graph).
        hoveredRef.current = d.kind === "cluster" ? null : d.alias;
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
      nodeSel.style("opacity", (d) => {
        const dim = !(hv == null || d.alias === hv || d.kind === "cluster");
        if (dim) return 0.16;
        // Settling (recently-terminal, fading) jobs sit at a calmer base so the
        // eye is drawn to the live in-flight work, not the finished tail.
        return d.kind === "job" && d.box?.lifecycle === "settling" ? 0.5 : 1;
      });
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
    const reduceMotion =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
    if (reduceMotion) {
      // Honour prefers-reduced-motion: advance the layout synchronously and
      // paint the settled state once, with no animated re-settle. Drag (direct
      // manipulation) is exempt and still works.
      sim.alpha(firstBuild ? 0.9 : 0.25).alphaDecay(0.1);
      for (let i = 0; i < 300 && sim.alpha() > sim.alphaMin(); i += 1) sim.tick();
      ticked();
      sim.stop();
    } else {
      sim
        .alpha(firstBuild ? 0.9 : 0.25)
        .alphaDecay(firstBuild ? 0.045 : 0.12)
        .alphaTarget(0)
        .restart();
    }
    simRef.current = sim;

    const dragBehavior = d3drag<SVGGElement, FlowNode>()
      // A pointer move under this many px still counts as a click, so a tiny
      // jitter while clicking a job does not suppress opening its JSON detail.
      .clickDistance(5)
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

    // ---------- moving "energy" particles ----------
    // Glowing dots travel producer → job → cluster along the active links so a
    // live job reads as energy in motion, not a static dot. Running/reducing
    // links carry two faster particles; queued/pending links carry one slow
    // drifting particle. Settling (fading) links carry none. Honours
    // prefers-reduced-motion: when set, no particles animate (the static
    // status glyphs already convey state).
    interface Particle {
      link: FlowLink;
      dur: number;
      offset: number;
    }
    const particleData: Particle[] = [];
    links.forEach((l) => {
      if (l.lifecycle === "settling") return;
      if (l.status === "running" || l.status === "reducing") {
        const dur = l.status === "reducing" ? 2400 : 1600;
        particleData.push({ link: l, dur, offset: 0 });
        particleData.push({ link: l, dur, offset: 0.5 });
      } else {
        // queued/pending: a single slow particle drifting toward the broker.
        particleData.push({ link: l, dur: 3000, offset: (spread01(l.id) + 0.5) % 1 });
      }
    });

    const particleSel = particleLayer
      .selectAll<SVGCircleElement, Particle>("circle")
      .data(particleData)
      .join("circle")
      .attr("class", "mf-particle")
      .attr("r", (p) =>
        p.link.status === "running" || p.link.status === "reducing" ? 2 : 1.5,
      )
      .attr("fill", (p) => jobTone(p.link.status, p.link.alias).accent)
      // `color` drives the CSS drop-shadow glow (currentColor) so the halo
      // matches the particle's own tone.
      .style("color", (p) => jobTone(p.link.status, p.link.alias).accent);

    // Evaluate the link's cubic bezier (same control points as the rendered
    // path) at parameter t, reading live node positions each frame.
    const cubicAt = (l: FlowLink, t: number): { x: number; y: number } => {
      const sx = l.source.x ?? 0;
      const sy = l.source.y ?? 0;
      const tx = l.target.x ?? 0;
      const ty = l.target.y ?? 0;
      const mx = (sx + tx) / 2;
      const u = 1 - t;
      const x = u * u * u * sx + 3 * u * u * t * mx + 3 * u * t * t * mx + t * t * t * tx;
      const y = u * u * u * sy + 3 * u * u * t * sy + 3 * u * t * t * ty + t * t * t * ty;
      return { x, y };
    };

    let particleRaf = 0;
    const animateParticles = (ts: number) => {
      const hv = hoveredRef.current;
      particleSel
        .attr("transform", (p) => {
          const t = (ts / p.dur + p.offset) % 1;
          const pt = cubicAt(p.link, t);
          return `translate(${pt.x},${pt.y})`;
        })
        .attr("opacity", (p) => {
          const t = (ts / p.dur + p.offset) % 1;
          const fade = Math.sin(Math.PI * t); // 0 at the ends, 1 at mid-flight
          const dim = hv && p.link.alias !== hv ? 0.12 : 1;
          return (0.15 + 0.8 * fade) * dim;
        });
      particleRaf = requestAnimationFrame(animateParticles);
    };
    if (!reduceMotion && particleData.length) {
      particleRaf = requestAnimationFrame(animateParticles);
    }

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
        .attr("stroke-width", (l) => {
          // Running/reducing links stay bright + thick regardless of age so a
          // long-running job's connection never fades to near-invisible (the
          // old age-only model made a live job look like it had vanished).
          if (l.lifecycle === "settling") return 0.8;
          if (l.status === "running" || l.status === "reducing") return 1.7;
          return ageStyle(l.born, now).w;
        })
        .attr("stroke-opacity", (l) => {
          const running = l.status === "running" || l.status === "reducing";
          const base =
            l.lifecycle === "settling" ? 0.12 : running ? 0.42 : ageStyle(l.born, now).op;
          const hv = hoveredRef.current;
          return hv && l.alias !== hv ? base * 0.18 : base;
        });
      nodeSel.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
      sessionG.each(function (a) {
        const members = membersByAlias.get(a) ?? [];
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

    // A 20s refetch can drop the submitter the pointer is currently over (React
    // does not fire mouseleave on a removed SVG node), which would leave
    // `hoveredRef` pinned to a now-absent alias and dim the WHOLE graph. Drop a
    // stale hover, then re-apply so an in-progress hover survives the rebuild.
    const aliasSet = new Set<string>([
      ...producerNodes.map((n) => n.alias),
      ...jobNodes.map((n) => n.alias),
    ]);
    if (hoveredRef.current && !aliasSet.has(hoveredRef.current)) hoveredRef.current = null;
    applyHover();

    return () => {
      sim.stop();
      cancelAnimationFrame(particleRaf);
    };
    // `selectedJobId` is intentionally NOT a dependency: it only changes the
    // selection ring, handled by the dedicated effect below. Including it here
    // would rebuild the whole SVG and restart the simulation on every job
    // click — the jarring re-settle this component is meant to avoid.
  }, [snapshot, size, subscriptions, onSelectBox, motionPrefTick]);

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
      <svg ref={svgRef} role="group" aria-label="Service Bus message flow constellation" />
    </div>
  );
}
