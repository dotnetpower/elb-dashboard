/**
 * NCBI deep-link builders for the sequence detail page.
 *
 * The "Related NCBI resources" panel pivots a researcher from a nuccore record
 * to the matching NCBI pages. These helpers centralise the URL construction so
 * the links stay robust and are unit-testable independent of the React page.
 *
 * Two correctness rules drive the shapes here:
 *  - Taxonomy uses the modern NCBI Datasets browser. The legacy
 *    `Taxonomy/Browser/wwwtax.cgi` page is being retired by NCBI (Fall 2026)
 *    and already renders a deprecation banner, so we link to the stable
 *    `datasets/taxonomy/<taxid>/` page instead.
 *  - Organism-scoped Entrez searches use the taxid form `txid<N>[Organism:exp]`
 *    when a taxid is known. An unquoted multi-word organism name with `[orgn]`
 *    binds the field tag only to the final token (e.g. `... coronavirus 2[orgn]`
 *    matches `2[orgn]`), which makes Entrez fail or mismatch. When no taxid is
 *    available we fall back to a quoted phrase `"<organism>"[Organism]`.
 */

const NCBI_DATASETS_TAXONOMY_BASE = "https://www.ncbi.nlm.nih.gov/datasets/taxonomy/";
const NCBI_NUCCORE_SEARCH_BASE = "https://www.ncbi.nlm.nih.gov/nuccore/?term=";

/**
 * Stable deep-link to a taxon on the modern NCBI Datasets Taxonomy browser.
 * Accepts a numeric taxid or its string form.
 */
export function ncbiTaxonomyUrl(taxid: number | string): string {
  return `${NCBI_DATASETS_TAXONOMY_BASE}${encodeURIComponent(String(taxid))}/`;
}

/**
 * Build the Entrez organism clause for a search term.
 *
 * Prefers `txid<N>[Organism:exp]` (exact, full subtree) when a taxid is known.
 * Falls back to a quoted phrase `"<organism>"[Organism]` so the field tag binds
 * to the whole name instead of only the trailing token. Returns `null` when
 * neither a taxid nor a usable organism name is available.
 */
export function ncbiOrganismClause(opts: {
  taxid: number | null;
  organism: string | null;
}): string | null {
  const { taxid, organism } = opts;
  if (taxid != null && Number.isFinite(taxid)) {
    return `txid${taxid}[Organism:exp]`;
  }
  const name = organism?.trim();
  if (name) {
    return `"${name}"[Organism]`;
  }
  return null;
}

/**
 * Build a nucleotide (nuccore) Entrez search scoped to an organism.
 *
 * Prefers the taxid form `txid<N>[Organism:exp]` (exact, includes the full
 * subtree). Falls back to a quoted organism phrase when only a name is known.
 * Returns `null` when neither a taxid nor a usable organism name is available.
 */
export function ncbiNucleotideByOrganismUrl(opts: {
  taxid: number | null;
  organism: string | null;
}): string | null {
  const clause = ncbiOrganismClause(opts);
  if (!clause) {
    return null;
  }
  return `${NCBI_NUCCORE_SEARCH_BASE}${encodeURIComponent(clause)}`;
}
