import { describe, expect, it } from "vitest";

import type { Finding } from "@/api/diagnostics";
import { severityRank } from "@/api/diagnostics";

import { RESOURCE_LABEL, groupByResource, sortBySeverity } from "./diagnosticsModel";

function finding(partial: Partial<Finding>): Finding {
  return {
    id: "x",
    category: "reliability",
    pillar: "Reliability",
    resource_kind: "aks",
    resource_name: "c1",
    severity: "ok",
    title: "t",
    detail: "d",
    recommendation: "",
    doc_url: "",
    rule_version: "1",
    expected_by_charter: false,
    observed: {},
    ...partial,
  };
}

describe("diagnostics model", () => {
  it("ranks severities most-actionable first", () => {
    expect(severityRank("critical")).toBeGreaterThan(severityRank("indeterminate"));
    expect(severityRank("indeterminate")).toBeGreaterThan(severityRank("warning"));
    expect(severityRank("warning")).toBeGreaterThan(severityRank("ok"));
    // Unknown values sort below everything known.
    expect(severityRank("nonsense")).toBeLessThan(severityRank("ok"));
  });

  it("orders resource groups by their most-severe finding", () => {
    const findings = [
      finding({ resource_kind: "storage", severity: "warning" }),
      finding({ resource_kind: "aks", severity: "critical" }),
      finding({ resource_kind: "api", severity: "ok" }),
    ];
    const groups = groupByResource(findings).map(([kind]) => kind);
    expect(groups).toEqual(["aks", "storage", "api"]);
  });

  it("sorts findings within a group most-severe first", () => {
    const findings = [
      finding({ id: "a", severity: "ok" }),
      finding({ id: "b", severity: "critical" }),
      finding({ id: "c", severity: "warning" }),
    ];
    expect(sortBySeverity(findings).map((f) => f.id)).toEqual(["b", "c", "a"]);
  });

  it("does not mutate the input array", () => {
    const findings = [finding({ id: "a", severity: "ok" }), finding({ id: "b", severity: "critical" })];
    const before = findings.map((f) => f.id);
    sortBySeverity(findings);
    expect(findings.map((f) => f.id)).toEqual(before);
  });

  it("labels every backend resource kind", () => {
    for (const kind of ["aks", "storage", "acr", "container_app", "api", "queue"]) {
      expect(RESOURCE_LABEL[kind]).toBeTruthy();
    }
  });
});
