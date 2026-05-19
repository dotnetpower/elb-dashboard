import { describe, expect, it } from "vitest";

import { classifyBlastResultFile, splitBlastResultFiles } from "./blastResultsModel";

import type { BlastResultFile } from "@/api/endpoints";

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
