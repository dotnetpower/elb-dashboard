/**
 * Tests for BlastStepTimeline/parseStepLog.ts.
 *
 * Responsibility: Lock the line-classification, timestamp shortening, noise
 * detection, summary/detail split and issue counting contracts that
 * `StepLogBlock.tsx` renders against.
 * Edit boundaries: Pure-function assertions only.
 * Key entry points: the `describe` blocks below.
 * Risky contracts: `severity` strings map 1:1 to `.step-log-text--*` CSS.
 * Validation: `cd web && npm test -- parseStepLog.test`.
 */
import { describe, expect, it } from "vitest";

import {
  classifySeverity,
  countIssues,
  isNoiseLine,
  parseLogLine,
  parseStepLog,
  shortenTimestamp,
  splitSummaryDetail,
} from "./parseStepLog";

describe("classifySeverity", () => {
  it("flags error lines", () => {
    expect(classifySeverity("ERROR something blew up")).toBe("error");
    expect(classifySeverity("✗ Submit failed")).toBe("error");
    expect(classifySeverity("Traceback (most recent call last)")).toBe("error");
  });

  it("flags warnings", () => {
    expect(classifySeverity("WARNING low disk")).toBe("warn");
    expect(classifySeverity("step was skipped")).toBe("warn");
  });

  it("flags success and headers", () => {
    expect(classifySeverity("✓ All steps completed.")).toBe("ok");
    expect(classifySeverity("--- Live Console Output ---")).toBe("header");
  });

  it("flags blast / tool lines", () => {
    expect(classifySeverity("elastic-blast submit")).toBe("blast");
    expect(classifySeverity("kubectl get pods")).toBe("blast");
    expect(classifySeverity("Number of sequences: 100")).toBe("blast");
  });

  it("defaults to plain text", () => {
    expect(classifySeverity("1/5 Writing configuration")).toBe("text");
  });
});

describe("isNoiseLine", () => {
  it("folds credential + HTTP pipeline chatter", () => {
    expect(isNoiseLine("ManagedIdentityCredential.get_token succeeded")).toBe(true);
    expect(isNoiseLine("Request URL: 'http://169.254.169.254/metadata/identity/oauth2/token'")).toBe(true);
    expect(isNoiseLine("    'Authorization': 'REDACTED'")).toBe(true);
    expect(isNoiseLine("    'User-Agent': 'azsdk-python-identity/1.0'")).toBe(true);
  });

  it("keeps genuine BLAST progress out of the noise bucket", () => {
    expect(isNoiseLine("1/5 Writing configuration")).toBe(false);
    expect(isNoiseLine("Splitting queries by effective search space")).toBe(false);
    expect(isNoiseLine("✓ Submitted successfully")).toBe(false);
  });
});

describe("shortenTimestamp", () => {
  it("reduces ISO timestamps to HH:MM:SS", () => {
    expect(shortenTimestamp("2026-06-05T06:20:01.473620Z")).toBe("06:20:01");
    expect(shortenTimestamp("[2026-06-05 06:20:01]")).toBe("06:20:01");
  });

  it("returns the input when there is no recognisable time", () => {
    expect(shortenTimestamp("2026-06-05")).toBe("2026-06-05");
  });
});

describe("parseLogLine", () => {
  it("splits timestamp from body and strips ANSI", () => {
    const line = parseLogLine("2026-06-05T06:20:01Z \x1B[32mINFO: ready\x1B[0m", 3);
    expect(line.n).toBe(3);
    expect(line.ts).toBe("2026-06-05T06:20:01Z");
    expect(line.body).toBe("INFO: ready");
    expect(line.severity).toBe("info");
  });
});

describe("splitSummaryDetail", () => {
  it("splits at the first header line", () => {
    const lines = parseStepLog(
      "Running elastic-blast submit...\n\n--- Live Console Output ---\n1/5 Writing config",
    );
    const { summary, detail } = splitSummaryDetail(lines);
    expect(summary.map((l) => l.body)).toEqual(["Running elastic-blast submit...", ""]);
    expect(detail[0].body).toBe("--- Live Console Output ---");
  });

  it("treats a leading header as all-detail", () => {
    const lines = parseStepLog("--- Console Output ---\nhello");
    const { summary, detail } = splitSummaryDetail(lines);
    expect(summary).toHaveLength(0);
    expect(detail).toHaveLength(2);
  });

  it("uses the first line as summary when there is no header", () => {
    const lines = parseStepLog("one\ntwo\nthree");
    const { summary, detail } = splitSummaryDetail(lines);
    expect(summary.map((l) => l.body)).toEqual(["one"]);
    expect(detail.map((l) => l.body)).toEqual(["two", "three"]);
  });

  it("keeps a short log entirely in the summary", () => {
    const lines = parseStepLog("only one line");
    const { summary, detail } = splitSummaryDetail(lines);
    expect(summary).toHaveLength(1);
    expect(detail).toHaveLength(0);
  });
});

describe("countIssues", () => {
  it("counts error and warning lines", () => {
    const lines = parseStepLog(
      "INFO ok\nERROR boom\nWARNING careful\n✗ failed again\nplain",
    );
    expect(countIssues(lines)).toEqual({ error: 2, warn: 1 });
  });
});
