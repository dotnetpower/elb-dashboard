// Short display names for long image paths
export const SHORT_NAMES: Record<string, string> = {
  "ncbi/elb": "elb (BLAST worker)",
  "ncbi/elasticblast-job-submit": "job-submit",
  "ncbi/elasticblast-query-split": "query-split",
  "elb-openapi": "openapi",
};

// All required images (worker, job-submit, query-split, openapi)
export const CORE_IMAGES = new Set([
  "ncbi/elb",
  "ncbi/elasticblast-job-submit",
  "ncbi/elasticblast-query-split",
  "elb-openapi",
]);

export function formatTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

export function statusLabel(s: string | undefined): string {
  if (!s) return "Building";
  if (s === "Queued") return "Starting";
  if (s === "Running") return "Building";
  return s;
}
