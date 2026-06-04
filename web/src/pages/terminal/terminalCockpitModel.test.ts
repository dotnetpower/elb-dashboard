import { describe, expect, it } from "vitest";

import {
  classifyCommand,
  COCKPIT_CHAPTERS,
  COCKPIT_WORKFLOWS,
  INNOVATION_CAPABILITIES,
  normaliseCommandForTerminalInsert,
  analysePastePayload,
  buildElbCfgCommand,
  deriveChapterSignalsFromActivity,
  deriveSessionChapters,
  ELB_CFG_FORM_DEFAULTS,
} from "./terminalCockpitModel";

describe("terminal cockpit model", () => {
  it("represents every proposed terminal capability", () => {
    expect(INNOVATION_CAPABILITIES.length).toBeGreaterThanOrEqual(35);
    expect(new Set(INNOVATION_CAPABILITIES.map((item) => item.id)).size).toBe(
      INNOVATION_CAPABILITIES.length,
    );
    // Honest two-tier split: every entry is either shipped or roadmap, both
    // tiers are populated, and the inflated three-tier labelling is gone.
    expect(INNOVATION_CAPABILITIES.every((item) => item.tier === "shipped" || item.tier === "roadmap")).toBe(true);
    const shipped = INNOVATION_CAPABILITIES.filter((item) => item.tier === "shipped");
    const roadmap = INNOVATION_CAPABILITIES.filter((item) => item.tier === "roadmap");
    expect(shipped.length).toBeGreaterThan(0);
    expect(roadmap.length).toBeGreaterThan(0);
    // Capabilities that actually shipped this work must not be mislabelled as
    // roadmap, and aspirational-only entries must not be mislabelled as shipped.
    expect(shipped.map((item) => item.id)).toEqual(
      expect.arrayContaining(["intent", "safe-run", "paste", "blast-triage", "maturity-ladder"]),
    );
    expect(roadmap.map((item) => item.id)).toEqual(
      expect.arrayContaining(["explain", "autopsy", "ai-boundary", "nl-builder"]),
    );
    expect(INNOVATION_CAPABILITIES.map((item) => item.id)).toEqual(
      expect.arrayContaining([
        "diagnostic-presets",
        "sample-context",
        "blast-triage",
        "controls",
        "evidence-summary",
        "cold-review",
        "maturity-ladder",
        "input-qc-gates",
        "db-provenance-gates",
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

  it("marks recognised commands with high confidence and unknown ones low", () => {
    expect(classifyCommand("kubectl delete namespace blast-prod").confidence).toBe("high");
    expect(classifyCommand("some-random-binary --flag").confidence).toBe("low");
  });

  it("picks the worst segment of a compound command and lowers confidence", () => {
    const analysis = classifyCommand("seqkit stats q.fa && kubectl delete pod x");
    expect(analysis.impact).toBe("destructive");
    expect(analysis.risk).toBe("high");
    // Compound commands cannot be judged with full certainty.
    expect(analysis.confidence).toBe("medium");
  });

  it("does not treat a 2>&1 redirect as a local write", () => {
    const analysis = classifyCommand("elastic-blast status 2>&1");
    expect(analysis.impact).not.toBe("local-write");
  });

  it("recognises a real output redirect as a local write", () => {
    const analysis = classifyCommand("seqkit stats q.fa > stats.txt");
    expect(analysis.impact).toBe("local-write");
  });

  it("treats shell builtins and no-ops as low-risk local reads", () => {
    for (const cmd of ["echo hello", "cd /tmp", "clear", "history"]) {
      const analysis = classifyCommand(cmd);
      expect(analysis.impact).toBe("local-read");
      expect(analysis.risk).toBe("low");
    }
  });

  it("treats az account set as a config switch, not a cloud mutation", () => {
    const analysis = classifyCommand("az account set --subscription sub-id");
    expect(analysis.impact).toBe("azure-read");
    expect(analysis.risk).not.toBe("high");
  });

  it("does not treat a redirect inside quotes as a local write", () => {
    const analysis = classifyCommand('echo "a > b"');
    expect(analysis.impact).not.toBe("local-write");
  });

  it("ignores leading environment-variable assignments", () => {
    const analysis = classifyCommand("FOO=bar echo hi");
    expect(analysis.impact).toBe("local-read");
    expect(analysis.risk).toBe("low");
  });

  it("rates kubectl exec as medium, not destructive", () => {
    const analysis = classifyCommand("kubectl exec -it pod-x -- bash");
    expect(analysis.impact).not.toBe("destructive");
    expect(analysis.risk).toBe("medium");
  });

  it("strips dangerous flags when proposing a safer kubectl delete preview", () => {
    const analysis = classifyCommand(
      "kubectl delete pod blast-x --force --grace-period=0 --now",
    );
    expect(analysis.impact).toBe("destructive");
    expect(analysis.saferCommand).toBe("kubectl get pod blast-x");
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

describe("buildElbCfgCommand", () => {
  it("emits only elb-cfg with the default output when fields are empty", () => {
    const cmd = buildElbCfgCommand({ ...ELB_CFG_FORM_DEFAULTS, program: "" });
    expect(cmd).toBe("elb-cfg -o ~/elastic-blast.ini");
  });

  it("includes set flags and omits empty ones", () => {
    const cmd = buildElbCfgCommand({
      ...ELB_CFG_FORM_DEFAULTS,
      db: "blast-db/16S/16S",
      queries: "q.fa",
      results: "run-1",
    });
    expect(cmd).toContain("--program blastn");
    expect(cmd).toContain("--db blast-db/16S/16S");
    expect(cmd).toContain("--queries q.fa");
    expect(cmd).toContain("--results run-1");
    expect(cmd).toContain("-o ~/elastic-blast.ini");
    expect(cmd).not.toContain("--region");
    expect(cmd).not.toContain("--machine-type");
  });

  it("shell-quotes values with spaces or special characters", () => {
    const cmd = buildElbCfgCommand({
      ...ELB_CFG_FORM_DEFAULTS,
      results: "run with space",
    });
    expect(cmd).toContain("--results 'run with space'");
  });

  it("omits the output flag when output is cleared", () => {
    const cmd = buildElbCfgCommand({ ...ELB_CFG_FORM_DEFAULTS, output: "" });
    expect(cmd).not.toContain("-o ");
  });
});

describe("analysePastePayload", () => {
  it("treats a bare single line as not multi-line", () => {
    const result = analysePastePayload("ls -lh");
    expect(result.isMultiline).toBe(false);
    expect(result.lineCount).toBe(1);
  });

  it("treats a single line with one trailing newline as not multi-line", () => {
    const result = analysePastePayload("elastic-blast submit\n");
    expect(result.isMultiline).toBe(false);
    expect(result.lineCount).toBe(1);
  });

  it("flags two or more command lines as multi-line", () => {
    const result = analysePastePayload("cd /data\nelastic-blast submit");
    expect(result.isMultiline).toBe(true);
    expect(result.lineCount).toBe(2);
  });

  it("flags a multi-line block with a trailing newline", () => {
    const result = analysePastePayload("cmd1\ncmd2\n");
    expect(result.isMultiline).toBe(true);
    expect(result.lineCount).toBe(2);
  });

  it("normalises CRLF and CR newlines", () => {
    expect(analysePastePayload("a\r\nb").isMultiline).toBe(true);
    expect(analysePastePayload("a\rb").isMultiline).toBe(true);
  });

  it("counts only non-empty lines but still flags blank-separated blocks", () => {
    const result = analysePastePayload("cmd1\n\ncmd3");
    expect(result.isMultiline).toBe(true);
    expect(result.lineCount).toBe(2);
  });

  it("reports the payload length", () => {
    expect(analysePastePayload("abc").length).toBe(3);
  });
});

describe("session chapter derivation", () => {
  it("marks the first unsatisfied chapter as active and earlier ones ready", () => {
    const chapters = deriveSessionChapters({
      azureSignedIn: true,
      stagedData: false,
      preparedAnalysis: false,
      ranSearch: false,
      reviewedOutputs: false,
    });
    const byId = Object.fromEntries(chapters.map((chapter) => [chapter.id, chapter.status]));
    expect(byId.auth).toBe("ready");
    expect(byId.stage).toBe("active");
    expect(byId.prepare).toBe("next");
  });

  it("preserves the static chapter id order", () => {
    const chapters = deriveSessionChapters({
      azureSignedIn: false,
      stagedData: false,
      preparedAnalysis: false,
      ranSearch: false,
      reviewedOutputs: false,
    });
    expect(chapters.map((chapter) => chapter.id)).toEqual(
      COCKPIT_CHAPTERS.map((chapter) => chapter.id),
    );
    // With nothing satisfied, the very first chapter is active.
    expect(chapters[0].status).toBe("active");
  });

  it("folds real activity into chapter signals", () => {
    const signals = deriveChapterSignalsFromActivity({
      azureSignedIn: true,
      executedCommands: ["seqkit stats q.fa", "elastic-blast submit"],
      hasTriageEvidence: false,
    });
    expect(signals.azureSignedIn).toBe(true);
    expect(signals.stagedData).toBe(true);
    expect(signals.ranSearch).toBe(true);
    expect(signals.reviewedOutputs).toBe(false);
  });
});

