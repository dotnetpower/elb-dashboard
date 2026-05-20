export type DisplayJobState =
  | "Pending"
  | "Running"
  | "Reducing"
  | "Completed"
  | "Failed"
  | "Unknown";

export interface JobRowView {
  jobId: string;
  displayId?: string;
  title: string;
  program: string;
  db: string;
  query: string;
  clusterName?: string | null;
  state: DisplayJobState;
  createdAt?: string | null;
  elapsedSec?: number | null;
  etaSec?: number | null;
  splitsDone?: number | null;
  splitsTotal?: number | null;
  note?: string | null;
}