import { describe, expect, it } from "vitest";

import type { K8sEvent } from "@/api/endpoints";

import { groupEvents, toEventLineView } from "./eventMapping";

function ev(overrides: Partial<K8sEvent>): K8sEvent {
  return {
    namespace: "default",
    name: "evt",
    type: "Normal",
    reason: "Pulled",
    message: "Successfully pulled image",
    count: 1,
    last_timestamp: new Date().toISOString(),
    involved_kind: "Pod",
    involved_name: "pod-x",
    source_component: "kubelet",
    source_host: "node-a",
    ...overrides,
  };
}

describe("classifyEvent (via toEventLineView)", () => {
  it("Warning + ERR_REASONS → err", () => {
    const v = toEventLineView(ev({ type: "Warning", reason: "Failed" }), 0);
    expect(v.kind).toBe("err");
  });
  it("Warning + WARN_REASONS → warn", () => {
    const v = toEventLineView(ev({ type: "Warning", reason: "BackOff" }), 0);
    expect(v.kind).toBe("warn");
  });
  it("Warning + unknown reason → warn", () => {
    const v = toEventLineView(ev({ type: "Warning", reason: "Mystery" }), 0);
    expect(v.kind).toBe("warn");
  });
  it("Normal + RemovingNode → info (not green check)", () => {
    const v = toEventLineView(
      ev({ type: "Normal", reason: "RemovingNode", involved_kind: "Node" }),
      0,
    );
    expect(v.kind).toBe("info");
  });
  it("Normal + NodeNotSchedulable → info", () => {
    const v = toEventLineView(
      ev({ type: "Normal", reason: "NodeNotSchedulable", involved_kind: "Node" }),
      0,
    );
    expect(v.kind).toBe("info");
  });
  it("Normal + ordinary reason → ok", () => {
    const v = toEventLineView(ev({ reason: "Pulled" }), 0);
    expect(v.kind).toBe("ok");
  });
});

describe("groupEvents", () => {
  it("collapses N RemovingNode events emitted for sibling vmss nodes into one row", () => {
    const ts = new Date().toISOString();
    const events: K8sEvent[] = Array.from({ length: 11 }, (_, i) =>
      ev({
        type: "Normal",
        reason: "RemovingNode",
        involved_kind: "Node",
        involved_name: `aks-blastp16v3-vmss00000${i}`,
        last_timestamp: ts,
      }),
    );
    const groups = groupEvents(events);
    expect(groups).toHaveLength(1);
    const [g] = groups;
    expect(g.kind).toBe("info");
    expect(g.count).toBe(11);
    expect(g.message).toMatch(/\[RemovingNode\]/);
    // Names with a long shared prefix should collapse to a `..` summary.
    expect(g.message).toMatch(/aks-blastp16v3-vmss\d+\.\.\d+/);
    // The group label should also include the (N) marker.
    expect(g.message).toContain("(11)");
  });

  it("keeps unrelated reasons in distinct groups", () => {
    const ts = new Date().toISOString();
    const events: K8sEvent[] = [
      ev({ type: "Normal", reason: "RemovingNode", involved_kind: "Node", involved_name: "a", last_timestamp: ts }),
      ev({ type: "Warning", reason: "FailedScheduling", involved_kind: "Pod", involved_name: "p", last_timestamp: ts }),
    ];
    const groups = groupEvents(events);
    expect(groups).toHaveLength(2);
    const reasons = groups.map((g) => g.message.match(/\[(\w+)\]/)?.[1]);
    expect(reasons.sort()).toEqual(["FailedScheduling", "RemovingNode"]);
  });

  it("respects the visible row cap and keeps newest groups", () => {
    const now = Date.now();
    const events: K8sEvent[] = Array.from({ length: 20 }, (_, i) =>
      ev({
        type: "Normal",
        reason: `Reason${i}`,
        involved_kind: "Pod",
        involved_name: `p${i}`,
        // Each event is in its own bucket (5 minutes apart).
        last_timestamp: new Date(now - i * 5 * 60_000).toISOString(),
      }),
    );
    const groups = groupEvents(events, 5);
    expect(groups).toHaveLength(5);
    // The first group should be the newest (i=0).
    expect(groups[0].message).toContain("Reason0");
  });

  it("includes the namespace prefix when not in default/kube-system", () => {
    const events: K8sEvent[] = [
      ev({
        type: "Warning",
        reason: "BackOff",
        involved_kind: "Pod",
        involved_name: "blast-1",
        namespace: "blast-jobs",
        last_timestamp: new Date().toISOString(),
      }),
    ];
    const [g] = groupEvents(events);
    expect(g.message).toContain("ns/blast-jobs");
  });

  it("does not include the namespace prefix for default or kube-system", () => {
    const events: K8sEvent[] = [
      ev({ namespace: "default", last_timestamp: new Date().toISOString() }),
      ev({ namespace: "kube-system", involved_name: "kube-proxy", last_timestamp: new Date().toISOString() }),
    ];
    const groups = groupEvents(events);
    for (const g of groups) {
      expect(g.message).not.toContain("ns/");
    }
  });

  it("ignores events with unparseable timestamps instead of crashing", () => {
    const events: K8sEvent[] = [
      ev({ last_timestamp: "not-a-date" }),
      ev({ last_timestamp: new Date().toISOString() }),
    ];
    const groups = groupEvents(events);
    expect(groups).toHaveLength(1);
  });

  it("for a single event keeps a sample of the original message", () => {
    const events: K8sEvent[] = [
      ev({
        type: "Warning",
        reason: "FailedMount",
        involved_kind: "Pod",
        involved_name: "blast-x",
        message: "MountVolume.SetUp failed for volume 'blast-share'",
        last_timestamp: new Date().toISOString(),
      }),
    ];
    const [g] = groupEvents(events);
    expect(g.message).toContain("MountVolume.SetUp");
  });
});
