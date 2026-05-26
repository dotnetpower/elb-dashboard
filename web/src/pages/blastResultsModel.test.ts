import { describe, expect, it } from "vitest";

import {
  classifyBlastResultFile,
  shouldShowNonTerminalJobError,
  splitBlastResultFiles,
} from "./blastResultsModel";

import type { BlastJobSummary, BlastResultFile } from "@/api/endpoints";

function file(name: string): BlastResultFile {
  return {
    name,
    file_id: name,
    size: 10,
    last_modified: "2026-05-19T00:00:00Z",
  };
}

describe("blast results file grouping", () => {
  it("keeps primary BLAST outputs separate from reports and diagnostics", () => {
    const files = [
      file("job-1/pods.txt"),
      file("job-1/split-results-manifest.json"),
      file("job-1/merged_results.out.gz"),
      file("job-1/merge-report.json"),
      file("job-1/worker.log"),
    ];

    const grouped = splitBlastResultFiles(files);

    expect(grouped.resultFiles.map((item) => item.name)).toEqual([
      "job-1/merged_results.out.gz",
    ]);
    expect(grouped.supportFiles.map((item) => item.name)).toEqual([
      "job-1/merge-report.json",
      "job-1/split-results-manifest.json",
    ]);
    expect(grouped.debugFiles.map((item) => item.name)).toEqual([
      "job-1/pods.txt",
      "job-1/worker.log",
    ]);
    expect(grouped.files.map((item) => item.name)).toEqual([
      "job-1/merged_results.out.gz",
    ]);
    expect(grouped.hasOnlyDebugFiles).toBe(false);
  });

  it("shows support artifacts when no primary output exists", () => {
    const grouped = splitBlastResultFiles([
      file("job-1/split-results-manifest.json"),
      file("job-1/pods.txt"),
    ]);

    expect(grouped.resultFiles).toHaveLength(0);
    expect(grouped.files.map((item) => item.name)).toEqual([
      "job-1/split-results-manifest.json",
    ]);
    expect(grouped.hasOnlyDebugFiles).toBe(false);
  });

  it("detects diagnostic-only listings", () => {
    const grouped = splitBlastResultFiles([
      file("job-1/pods.txt"),
      file("job-1/run.log"),
    ]);

    expect(grouped.files.map((item) => item.name)).toEqual([
      "job-1/pods.txt",
      "job-1/run.log",
    ]);
    expect(grouped.hasOnlyDebugFiles).toBe(true);
  });

  it("classifies common BLAST output suffixes as primary results", () => {
    expect(classifyBlastResultFile(file("job-1/result.out"))).toBe("result");
    expect(classifyBlastResultFile(file("job-1/result.xml.gz"))).toBe("result");
    expect(classifyBlastResultFile(file("job-1/result.asn"))).toBe("result");
  });
});

describe("blast job error display", () => {
  function job(overrides: Partial<BlastJobSummary>): BlastJobSummary {
    return {
      job_id: "job-1",
      job_title: "job-1",
      program: "blastn",
      db: "16S_ribosomal_RNA",
      status: "running",
      phase: "waiting_for_submit_slot",
      created_at: "2026-05-26T00:00:00Z",
      updated_at: "2026-05-26T00:00:10Z",
      ...overrides,
    };
  }

  it("treats submit-slot contention as a queued wait, not an error", () => {
    expect(
      shouldShowNonTerminalJobError(
        job({ error_code: "blast_submit_lock_busy", error: "blast_submit_lock_busy" }),
        "waiting_for_submit_slot",
      ),
    ).toBe(false);
  });

  it("keeps unexpected running job errors visible", () => {
    expect(
      shouldShowNonTerminalJobError(
        job({
          phase: "status_unavailable",
          error_code: "k8s_unavailable",
          error: "k8s_unavailable",
        }),
        "status_unavailable",
      ),
    ).toBe(true);
  });
});
