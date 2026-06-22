import { describe, expect, it } from "vitest";

import {
  WORKFLOW_EXPORT_FORMATS,
  workflowExportFilename,
  type WorkflowExportFormat,
} from "./workflowExportModel";

describe("workflowExportModel", () => {
  it("covers exactly the four backend-supported formats", () => {
    // Mirrors api/services/blast/workflow_export.py SUPPORTED_WORKFLOW_FORMATS.
    const formats = WORKFLOW_EXPORT_FORMATS.map((m) => m.format).sort();
    expect(formats).toEqual(["cwl", "nextflow", "snakemake", "wdl"]);
  });

  it("maps each format to the backend _FORMAT_FILENAMES value", () => {
    // Keep in sync with api/services/blast/workflow_export.py _FORMAT_FILENAMES.
    expect(workflowExportFilename("nextflow")).toBe("main.nf");
    expect(workflowExportFilename("snakemake")).toBe("Snakefile");
    expect(workflowExportFilename("cwl")).toBe("blast_submit.cwl");
    expect(workflowExportFilename("wdl")).toBe("blast_submit.wdl");
  });

  it("filename matches the metadata entry for every format", () => {
    for (const meta of WORKFLOW_EXPORT_FORMATS) {
      expect(workflowExportFilename(meta.format)).toBe(meta.filename);
    }
  });

  it("throws on an unknown format so a typo surfaces in tests", () => {
    expect(() =>
      workflowExportFilename("airflow" as WorkflowExportFormat),
    ).toThrow(/Unknown workflow export format/);
  });

  it("every format has a non-empty label and description", () => {
    for (const meta of WORKFLOW_EXPORT_FORMATS) {
      expect(meta.label.trim().length).toBeGreaterThan(0);
      expect(meta.description.trim().length).toBeGreaterThan(0);
    }
  });
});
