/**
 * layout — broker box sizing for the message-flow visualization.
 *
 * Box width is proportional to the log of the query sequence length so a
 * 12 kb query is visibly wider than a 0.4 kb one without a 30x query making a
 * 30x box. A null size (unknown query length) renders at the minimum width so
 * the UI never fabricates a length it does not have.
 */

const MIN_WIDTH = 56;
const MAX_WIDTH = 240;
const LOG_SCALE = 40;

export function boxWidth(querySize: number | null | undefined): number {
  if (querySize == null || querySize <= 0) return MIN_WIDTH;
  const scaled = MIN_WIDTH + Math.log10(querySize + 1) * LOG_SCALE;
  return Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, scaled)));
}

/** Human-readable query length label, e.g. "12.0k letters" or "—". */
export function querySizeLabel(querySize: number | null | undefined): string {
  if (querySize == null || querySize <= 0) return "—";
  if (querySize >= 1000) return `${(querySize / 1000).toFixed(1)}k letters`;
  return `${querySize} letters`;
}
