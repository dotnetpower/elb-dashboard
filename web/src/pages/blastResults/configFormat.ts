/**
 * Formatters for the BLAST job "Run details" parameter rows.
 *
 * These turn a config_snapshot (the submitted BLAST options, now captured for
 * queue/API jobs too) into the human strings the details grid renders: the
 * effective output format, the taxonomy include/exclude filter, and a compact
 * run-time. Kept pure + separate so they are unit-testable without React.
 */

type Config = Record<string, unknown> | undefined | null;

/** Effective output format: the multi-token specifier when present, else the bare code. */
export function formatOutfmt(config: Config): string {
  if (!config) return "—";
  const additional = String(config.additional_options ?? "");
  // additional_options carries the enriched specifier, e.g.
  // `-outfmt "7 std staxids sscinames stitle qcovs"` or `-outfmt 7 std staxids`.
  const match = additional.match(/-outfmt\s+"([^"]+)"|-outfmt\s+(\S+(?:\s+\S+)*)/);
  if (match) {
    const spec = (match[1] ?? match[2] ?? "").trim();
    if (spec) return spec;
  }
  const bare = config.outfmt;
  return bare != null && bare !== "" ? String(bare) : "—";
}

/** Taxonomy include/exclude filter label, or null when no taxid filter was set. */
export function taxonomyFilterLabel(config: Config): string | null {
  if (!config) return null;
  // Explicit BLAST flags win (the raw exclude/include taxids), then the
  // dashboard's taxid + is_inclusive representation.
  const negative = config.negative_taxids;
  if (negative != null && negative !== "") return `exclude taxid ${String(negative)}`;
  const positive = config.taxids;
  if (positive != null && positive !== "") return `include taxid ${String(positive)}`;
  const taxid = config.taxid;
  if (taxid == null || taxid === "") return null;
  // is_inclusive === false => exclude (-negative_taxids); otherwise include.
  const mode = config.is_inclusive === false ? "exclude" : "include";
  return `${mode} taxid ${String(taxid)}`;
}

/** Compact seconds → "Ns" / "Mm Ns"; "—" when not a finite number. */
export function formatRunSeconds(value: unknown): string {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 60) return `${Math.round(n)}s`;
  const minutes = Math.floor(n / 60);
  const seconds = Math.round(n % 60);
  return `${minutes}m ${seconds}s`;
}

/** True when the job came from outside the dashboard UI (queue / external API). */
export function isExternalJob(submissionSource: string | undefined): boolean {
  return !!submissionSource && submissionSource !== "dashboard";
}
