import { describe, expect, it } from "vitest";

import {
  classifyCommand,
  COCKPIT_CHAPTERS,
  COCKPIT_WORKFLOWS,
  INNOVATION_CAPABILITIES,
  normaliseCommandForTerminalInsert,
} from "./terminalCockpitModel";

describe("terminal cockpit model", () => {
  it("represents every proposed terminal capability", () => {
    expect(INNOVATION_CAPABILITIES.length).toBeGreaterThanOrEqual(35);
    expect(new Set(INNOVATION_CAPABILITIES.map((item) => item.id)).size).toBe(
      INNOVATION_CAPABILITIES.length,
    );
    expect(INNOVATION_CAPABILITIES.some((item) => item.status === "live")).toBe(true);
    expect(INNOVATION_CAPABILITIES.some((item) => item.status === "guarded")).toBe(true);
    expect(INNOVATION_CAPABILITIES.some((item) => item.status === "foundation")).toBe(true);
    expect(INNOVATION_CAPABILITIES.map((item) => item.id)).toEqual(
      expect.arrayContaining([
        "diagnostic-presets",
        "sample-context",
        "blast-triage",
        "controls",
        "evidence-summary",
      ]),
    );
  });

  it("classifies destructive commands as high risk", () => {
    const analysis = classifyCommand("kubectl delete namespace blast-prod");

    expect(analysis.impact).toBe("destructive");
    expect(analysis.risk).toBe("high");
    expect(analysis.saferCommand).toBe("kubectl get namespace blast-prod");
  });

  it("classifies Azure mutations as high risk", () => {
    const analysis = classifyCommand("az acr build --registry rg --image app:latest .");

    expect(analysis.impact).toBe("azure-write");
    expect(analysis.risk).toBe("high");
    expect(analysis.checks).toContain("Run az account show -o table.");
  });

  it("classifies local inspection as low risk", () => {
    const analysis = classifyCommand("seqkit stats *.fa");

    expect(analysis.impact).toBe("local-read");
    expect(analysis.risk).toBe("low");
  });

  it("keeps workflow and chapter ids stable", () => {
    expect(COCKPIT_WORKFLOWS.map((workflow) => workflow.id)).toContain("login");
    expect(COCKPIT_CHAPTERS.map((chapter) => chapter.id)).toEqual([
      "auth",
      "stage",
      "prepare",
      "run",
      "review",
    ]);
  });

  it("normalises pasted commands before terminal insertion", () => {
    expect(normaliseCommandForTerminalInsert("az account show\nrm -rf /tmp/x\u0007")).toBe(
      "az account show rm -rf /tmp/x",
    );
  });
});
