/**
 * MessageFlowConstellation — the "Closed Loop (A4)" force-graph rendering of
 * the Service Bus message flow.
 *
 * Four stages: Actors (left) → Queue box → Workers (queue consumers / AKS
 * clusters) → Topic box (right). A submitter is BOTH a producer and a
 * completion subscriber, so the flow is a closed loop: the completion sweeps
 * back over the top from the Topic box to the submitting actor (the dashed loop
 * arc). Jobs are force-positioned by lifecycle: queued jobs settle into the
 * Queue box, running jobs between the Queue and Workers, completed (settling)
 * jobs into the Topic box. Actors are api-dominant (rounded-square glyph) with
 * the occasional human user (circle), and carry a "producer + subscriber" label
 * once they have completed work. Hovering a submitter surfaces its session
 * group (jobs sharing an alias), brightens its completion loop, and dims the
 * rest. Clicking a job calls `onSelectBox` so the parent can open the JSON
 * detail modal.
 *
 * The completion loop maps a real per-actor completion count (claim-check
 * pattern) — it is NOT a claim that the submitter owns a named Service Bus
 * subscription; the named subscriptions are the dashboard's own system
 * subscribers, shown inside the Topic box ("SYSTEM SUBS").
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
  /** Completed (settling) jobs this submitter has — drives the dual-role
   *  "producer + subscriber" label and the completion-loop arc. */
  completed?: number;
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

/**
 * A completion-loop arc: the over-the-top return path from the Topic back to a
 * submitter that has completed jobs. It models the claim-check completion
 * notification (a submitter is BOTH a producer and a completion subscriber),
 * mapping a real per-actor completion count — NOT a claim that the submitter
 * owns a named Service Bus subscription (those are the system subs in the Topic).
 */
interface LoopArc {
  id: string;
  alias: string;
  actor: FlowNode;
  count: number;
  lane: number;
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

    // ----- geometry: four stages — Actors | Queue box | Workers | Topic box.
    // A submitter is both a producer and a completion subscriber, so the flow
    // is a closed loop: the completion sweeps back over the top (headroom in
    // by0) from the Topic box to the Actor. Variable names are kept from the
    // original single-broker layout so the simulation/tick code stays stable;
    // their meaning is now "Queue box" (bx0..bx1) plus a separate Topic box
    // (tx0..tx1).
    const bx0 = w * 0.3; // queue box left
    const bx1 = w * 0.46; // queue box right (narrower than the old broker)
    const by0 = 52; // headroom for the over-the-top completion loop
    const by1 = h - 22;
    const bw = bx1 - bx0;
    const bh = by1 - by0;
    const cx = (bx0 + bx1) / 2;
    const cy = (by0 + by1) / 2;
    const mid = (by0 + by1) / 2;
    const prodX = w * 0.1; // actors
    const clusX = w * 0.63; // workers (queue consumers)
    const tx0 = w * 0.8; // topic box left
    const tx1 = w * 0.96; // topic box right
    const tcx = (tx0 + tx1) / 2;
    const tbw = tx1 - tx0;
    const loopY = 14; // apex of the completion-loop arcs

    const svg = select(svgEl);
    svg.selectAll("*").remove();
    // A concise summary for screen readers (the per-node detail lives on the
    // focusable job buttons and the producer/cluster titles).
    svg.attr(
      "aria-label",
      `Service Bus message flow: ${producers.length} actor${
        producers.length === 1 ? "" : "s"
      }, ${broker.length} active job${broker.length === 1 ? "" : "s"}, ${
        clusters.length
      } worker cluster${clusters.length === 1 ? "" : "s"}`,
    );

    // Completion-loop arrowhead (points into the actor).
    svg
      .append("defs")
      .append("marker")
      .attr("id", "mf-loop-arrow")
      .attr("viewBox", "0 0 8 8")
      .attr("refX", 7)
      .attr("refY", 4)
      .attr("markerWidth", 7)
      .attr("markerHeight", 7)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,1 L6,4 L0,7")
      .attr("fill", "none")
      .attr("stroke", "var(--text-muted)")
      .attr("stroke-width", 1.2);

    // ---------- static Queue + Topic boxes + labels ----------
    // Decorative chrome — hidden from assistive tech so the only exposed nodes
    // are the interactive job buttons (added later with role=button).
    const boundary = svg.append("g").attr("aria-hidden", "true");
    // Queue box (requests)
    boundary
      .append("rect")
      .attr("x", bx0)
      .attr("y", by0)
      .attr("width", bw)
      .attr("height", bh)
      .attr("rx", 12)
      .attr("fill", "rgba(255,255,255,0.012)")
      .attr("stroke", "var(--border-medium)")
      .attr("stroke-width", 1.1);
    // Topic box (completions + system subscriptions)
    boundary
      .append("rect")
      .attr("x", tx0)
      .attr("y", by0)
      .attr("width", tbw)
      .attr("height", bh)
      .attr("rx", 12)
      .attr("fill", "rgba(255,255,255,0.012)")
      .attr("stroke", "var(--border-medium)")
      .attr("stroke-width", 1.1);

    const diamond = (x: number, y: number, r: number) =>
      `M${x},${y - r} L${x + r},${y} L${x},${y + r} L${x - r},${y} Z`;
    // Submitter aliases are UPNs (can be long) and cluster names can be long
    // too; clip them so a wide label does not run off the SVG edge.
    const truncate = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
    const labels = svg.append("g").attr("aria-hidden", "true");
    // Queue box label (diamond glyph)
    labels
      .append("path")
      .attr("d", diamond(bx0 + 14, by0 + 14, 5))
      .attr("fill", "var(--bg-tertiary)")
      .attr("stroke", "var(--accent)")
      .attr("stroke-width", 1);
    labels
      .append("text")
      .attr("x", bx0 + 24)
      .attr("y", by0 + 17)
      .attr("font-size", 10)
      .attr("fill", "var(--text-muted)")
      .text(truncate(snapshot.request_queue || "requests", 16));
    // Topic box label (square glyph)
    labels
      .append("rect")
      .attr("x", tx0 + 10)
      .attr("y", by0 + 9)
      .attr("width", 10)
      .attr("height", 10)
      .attr("rx", 2)
      .attr("fill", "var(--bg-tertiary)")
      .attr("stroke", "var(--teal)")
      .attr("stroke-width", 1);
    labels
      .append("text")
      .attr("x", tx0 + 24)
      .attr("y", by0 + 17)
      .attr("font-size", 10)
      .attr("fill", "var(--text-muted)")
      .text(truncate(snapshot.completion_topic || "completions", 16));
    // System subscribers (named SB subscriptions) live inside the Topic box,
    // clearly distinct from the Actors so a submitter is never read as "owns
    // this subscription".
    if (subscriptions.length) {
      const baseY = by1 - 12 - (subscriptions.length - 1) * 16;
      labels
        .append("text")
        .attr("x", tx0 + 10)
        .attr("y", baseY - 12)
        .attr("font-size", 8.5)
        .attr("font-weight", 600)
        .attr("letter-spacing", "0.04em")
        .attr("fill", "var(--text-faint)")
        .text("SYSTEM SUBS");
      subscriptions.forEach((s, i) => {
        const gx = tx0 + 14;
        const gy = by1 - 12 - i * 16;
        if (gy < by0 + 30) return;
        labels
          .append("circle")
          .attr("cx", gx)
          .attr("cy", gy)
          .attr("r", 3.5)
          .attr("fill", "var(--teal)")
          .attr("opacity", 0.8);
        labels
          .append("text")
          .attr("x", gx + 9)
          .attr("y", gy + 3)
          .attr("text-anchor", "start")
          .attr("font-size", 9)
          .attr("fill", "var(--text-faint)")
          .text(truncate(s, 14));
      });
    }

    // column captions (with sub-labels naming the dual role / collision fix)
    (
      [
        [prodX, "Actors", "produce + subscribe"],
        [cx, "Queue", "requests"],
        [clusX, "Workers", "queue consumers"],
        [tcx, "Topic", "completions"],
      ] as [number, string, string][]
    ).forEach(([x, t, sub]) => {
      svg
        .append("text")
        .attr("class", "mf-col-label")
        .attr("x", x)
        .attr("y", 18)
        .attr("text-anchor", "middle")
        .text(t);
      svg
        .append("text")
        .attr("class", "mf-col-sublabel")
        .attr("x", x)
        .attr("y", 31)
        .attr("text-anchor", "middle")
        .text(sub);
    });

    // ---------- nodes ----------
    const cache = posCacheRef.current;
    // Per-submitter completed (settling) job count drives the dual-role label
    // and the completion-loop arc back to that actor.
    const completedByAlias = new Map<string, number>();
    broker.forEach((b) => {
      if (b.lifecycle === "settling")
        completedByAlias.set(b.alias, (completedByAlias.get(b.alias) ?? 0) + 1);
    });
    const producerNodes: FlowNode[] = producers.map((p, i) => ({
      id: "p:" + p.alias,
      kind: "producer",
      alias: p.alias,
      pkind: producerKind(p.sources),
      count: p.job_count,
      completed: completedByAlias.get(p.alias) ?? 0,
      fx: prodX,
      fy: by0 + 6 + ((i + 0.5) / Math.max(1, producers.length)) * (by1 - by0 - 12),
    }));
    const clusterNodes: FlowNode[] = clusters.map((c, i) => ({
      id: "c:" + (c.cluster_name || "unassigned"),
      kind: "cluster",
      alias: "\x00cluster",
      clusterName: c.cluster_name,
      running: c.running,
      queued: c.queued,
      fx: clusX,
      fy: by0 + 6 + ((i + 0.5) / Math.max(1, clusters.length)) * (by1 - by0 - 12),
    }));
    const jobNodes: FlowNode[] = broker.map((b) => {
      const cached = cache.get(b.job_id);
      // First-frame home column by lifecycle/status: settling → Topic box,
      // waiting → Queue box, else (running/reducing) → between Workers and Queue.
      const settling = b.lifecycle === "settling";
      const waiting = b.status === "queued" || b.status === "pending";
      const homeX = settling ? tcx : waiting ? cx : clusX - w * 0.06;
      const span = settling ? tbw : bw;
      return {
        id: b.job_id,
        kind: "job",
        alias: b.alias,
        box: b,
        status: b.status,
        born: bornMs(b.created_at),
        r: jobRadius(b.query_size),
        x: cached?.x ?? homeX + spread01(b.job_id) * span * 0.5,
        y: cached?.y ?? cy + spread01(b.job_id + "y") * bh * 0.5,
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

    // ---------- completion-loop arcs (Topic → actor) ----------
    // One aggregate arc per actor that has completed jobs, sweeping over the
    // top from the Topic box back to the actor. Always visible (faint) so the
    // dual producer+subscriber role is STRUCTURAL, not hover-only. Honest: it
    // maps a real per-actor completion count, NOT a named SB subscription.
    const loopArcs: LoopArc[] = producerNodes
      .filter((p) => (p.completed ?? 0) > 0)
      .map((p, i) => ({
        id: "loop:" + p.alias,
        alias: p.alias,
        actor: p,
        count: p.completed ?? 0,
        lane: i,
      }));

    // ---------- layers ----------
    // Links, loop arcs and session hulls are decorative; only the node layer
    // carries the focusable job buttons, so hide the rest from assistive tech.
    const loopLayer = svg.append("g").attr("fill", "none").attr("aria-hidden", "true");
    const linkLayer = svg
      .append("g")
      .attr("fill", "none")
      .attr("stroke-linecap", "round")
      .attr("aria-hidden", "true");
    const sessionLayer = svg.append("g").attr("aria-hidden", "true");
    const nodeLayer = svg.append("g");

    const linkSel = linkLayer
      .selectAll<SVGPathElement, FlowLink>("path")
      .data(links, (d) => d.id)
      .join("path")
      .attr("stroke", (d) => aliasTone(d.alias).accent);

    const loopSel = loopLayer
      .selectAll<SVGPathElement, LoopArc>("path")
      .data(loopArcs, (d) => d.id)
      .join("path")
      .attr("stroke", (d) => aliasTone(d.alias).accent)
      .attr("stroke-width", 1.3)
      .attr("stroke-dasharray", "4 5")
      .attr("marker-end", "url(#mf-loop-arrow)");

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
          const hasCompleted = (d.completed ?? 0) > 0;
          sel
            .append("text")
            .attr("x", -14)
            .attr("dy", hasCompleted ? "-0.15em" : "0.32em")
            .attr("text-anchor", "end")
            .attr("font-size", 11)
            .attr("fill", "var(--text-primary)")
            .text(`${truncate(d.alias, 22)}${d.pkind === "api" ? " ·api" : ""} (${d.count})`);
          // Dual role: a submitter that has completed jobs is BOTH a producer
          // and a completion subscriber (the loop arc returns to it).
          if (hasCompleted) {
            sel
              .append("text")
              .attr("x", -14)
              .attr("dy", "1.05em")
              .attr("text-anchor", "end")
              .attr("font-size", 8.5)
              .attr("fill", "var(--text-faint)")
              .text("producer + subscriber");
          }
          sel
            .append("title")
            .text(
              `${d.alias} (${d.pkind}) — ${d.count} active job${d.count === 1 ? "" : "s"}${
                hasCompleted ? `, ${d.completed} completed (subscriber)` : ""
              }`,
            );
          sel
            .attr("role", "img")
            .attr(
              "aria-label",
              `${d.pkind} actor ${d.alias}, ${d.count} active job${d.count === 1 ? "" : "s"}${
                hasCompleted ? `, also subscribes to ${d.completed} completion${d.completed === 1 ? "" : "s"}` : ""
              }`,
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
      loopSel
        .attr("opacity", (d) => (hv == null ? 0.3 : d.alias === hv ? 0.95 : 0.05))
        .attr("stroke-width", (d) => (hv === d.alias ? 1.8 : 1.3));
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
    // Removed for the closed-loop (A4) design: the layout is intentionally calm
    // and static; status is conveyed by the node glyphs (queued ring, running
    // halo, completed check) and the completion-loop arc, not travelling dots.

    function ticked() {
      const now = Date.now();
      jobNodes.forEach((d) => {
        // Clamp each job into the box/column its lifecycle belongs to: settling
        // (completed) → Topic box, waiting → Queue box, else (running) between
        // the Queue box and the Workers column.
        const settling = d.box?.lifecycle === "settling";
        const waiting = d.status === "queued" || d.status === "pending";
        let lo: number;
        let hi: number;
        if (settling) {
          lo = tx0 + 6;
          hi = tx1 - 6;
        } else if (waiting) {
          lo = bx0 + 6;
          hi = bx1 - 6;
        } else {
          lo = bx1 + 10;
          hi = tx0 - 10;
        }
        d.x = Math.max(lo, Math.min(hi, d.x ?? cx));
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
      // Completion-loop arcs: from the Topic box top, sweep over the top of the
      // stage, and arrive just above the actor (lanes stagger the apex so
      // several actors' loops do not collapse onto one line).
      loopSel.attr("d", (d) => {
        const sx = tcx;
        const sy = by0;
        const ex = prodX;
        const ey = (d.actor.fy ?? d.actor.y ?? cy) - 12;
        const apexY = loopY + d.lane * 7;
        return `M${sx},${sy} C${sx},${apexY} ${ex},${apexY} ${ex},${ey}`;
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
