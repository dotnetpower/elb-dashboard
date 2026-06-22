/**
 * workflowExportModel — pure metadata for the per-job pipeline export menu (#57 R3).
 *
 * The backend route `GET /api/blast/jobs/{id}/export?format=…`
 * (`api/services/blast/workflow_export.py`) renders a self-contained Nextflow /
 * Snakemake / CWL / WDL module that re-submits the job's exact parameter set via
 * one `POST /api/blast/jobs` call. This module mirrors that backend's
 * `SUPPORTED_WORKFLOW_FORMATS` + `_FORMAT_FILENAMES` so the menu, the download
 * filename, and a guard test stay in sync with the server contract.
 *
 * Pure data + one pure helper — no React, no network — so it is unit-testable in
 * the logic-only frontend suite.
 */

import type { WorkflowExportFormat } from "@/api/endpoints";

export type { WorkflowExportFormat };

export interface WorkflowExportFormatMeta {
  format: WorkflowExportFormat;
  /** Menu label. */
  label: string;
  /** One-line description shown under the label. */
  description: string;
  /** Download filename — must match the backend `_FORMAT_FILENAMES`. */
  filename: string;
}

export const WORKFLOW_EXPORT_FORMATS: readonly WorkflowExportFormatMeta[] = [
  {
    format: "nextflow",
    label: "Nextflow",
    description: "main.nf — process that re-submits via POST /api/blast/jobs",
    filename: "main.nf",
  },
  {
    format: "snakemake",
    label: "Snakemake",
    description: "Snakefile — rule that re-submits this job's parameters",
    filename: "Snakefile",
  },
  {
    format: "cwl",
    label: "CWL",
    description: "blast_submit.cwl — CommandLineTool wrapping the submit call",
    filename: "blast_submit.cwl",
  },
  {
    format: "wdl",
    label: "WDL",
    description: "blast_submit.wdl — task that re-submits this job's parameters",
    filename: "blast_submit.wdl",
  },
] as const;

const _FILENAME_BY_FORMAT: Record<WorkflowExportFormat, string> =
  Object.fromEntries(
    WORKFLOW_EXPORT_FORMATS.map((meta) => [meta.format, meta.filename]),
  ) as Record<WorkflowExportFormat, string>;

/**
 * Resolve the download filename for a workflow format. Mirrors the backend
 * `_FORMAT_FILENAMES`; throws on an unknown format so a typo surfaces in tests
 * rather than downloading a mis-named file.
 */
export function workflowExportFilename(format: WorkflowExportFormat): string {
  const filename = _FILENAME_BY_FORMAT[format];
  if (!filename) {
    throw new Error(`Unknown workflow export format: ${format}`);
  }
  return filename;
}
