/**
 * Curated catalogue of NCBI taxa that researchers routinely target with
 * elastic-blast. Used by the taxonomy filter for *instant* client-side
 * autocomplete suggestions: matches appear as you type with zero latency,
 * while live NCBI E-utilities results merge in behind them.
 *
 * Keep this list small (~40 entries). Long-tail queries are handled by the
 * live search backend.
 */

export interface CommonTaxon {
  taxid: number;
  scientific_name: string;
  common_name: string | null;
  rank: string;
  /** Pre-computed lowercase name + synonyms haystack used by filtering. */
  search_text: string;
  /** Higher values rank earlier when multiple entries match. */
  priority: number;
}

interface Seed {
  taxid: number;
  scientific_name: string;
  common_name?: string | null;
  rank: string;
  synonyms?: string[];
  priority?: number;
}

const SEEDS: Seed[] = [
  // Model organisms
  { taxid: 9606, scientific_name: "Homo sapiens", common_name: "human", rank: "species", priority: 100 },
  { taxid: 10090, scientific_name: "Mus musculus", common_name: "house mouse", rank: "species", priority: 95 },
  { taxid: 10116, scientific_name: "Rattus norvegicus", common_name: "Norway rat", rank: "species", priority: 90 },
  { taxid: 7227, scientific_name: "Drosophila melanogaster", common_name: "fruit fly", rank: "species", priority: 90 },
  { taxid: 6239, scientific_name: "Caenorhabditis elegans", common_name: "roundworm", rank: "species", priority: 90 },
  { taxid: 7955, scientific_name: "Danio rerio", common_name: "zebrafish", rank: "species", priority: 90 },
  { taxid: 4932, scientific_name: "Saccharomyces cerevisiae", common_name: "baker yeast", rank: "species", priority: 90 },
  { taxid: 4896, scientific_name: "Schizosaccharomyces pombe", common_name: "fission yeast", rank: "species", priority: 70 },
  { taxid: 3702, scientific_name: "Arabidopsis thaliana", common_name: "thale cress", rank: "species", priority: 85 },
  { taxid: 562, scientific_name: "Escherichia coli", common_name: "E. coli", rank: "species", synonyms: ["ecoli"], priority: 95 },

  // Vertebrates / livestock
  { taxid: 9913, scientific_name: "Bos taurus", common_name: "cattle", rank: "species", priority: 70 },
  { taxid: 9823, scientific_name: "Sus scrofa", common_name: "pig", rank: "species", priority: 70 },
  { taxid: 9031, scientific_name: "Gallus gallus", common_name: "chicken", rank: "species", priority: 70 },
  { taxid: 9615, scientific_name: "Canis lupus familiaris", common_name: "dog", rank: "subspecies", priority: 65 },
  { taxid: 9685, scientific_name: "Felis catus", common_name: "cat", rank: "species", priority: 60 },
  { taxid: 9544, scientific_name: "Macaca mulatta", common_name: "rhesus macaque", rank: "species", priority: 70 },
  { taxid: 9598, scientific_name: "Pan troglodytes", common_name: "chimpanzee", rank: "species", priority: 65 },

  // Plants
  { taxid: 4530, scientific_name: "Oryza sativa", common_name: "rice", rank: "species", priority: 75 },
  { taxid: 4577, scientific_name: "Zea mays", common_name: "maize", rank: "species", priority: 75 },
  { taxid: 4565, scientific_name: "Triticum aestivum", common_name: "bread wheat", rank: "species", priority: 70 },

  // Bacteria
  { taxid: 1423, scientific_name: "Bacillus subtilis", rank: "species", priority: 70 },
  { taxid: 1773, scientific_name: "Mycobacterium tuberculosis", common_name: "TB", rank: "species", synonyms: ["mtb"], priority: 80 },
  { taxid: 1280, scientific_name: "Staphylococcus aureus", common_name: "S. aureus", rank: "species", priority: 80 },
  { taxid: 287, scientific_name: "Pseudomonas aeruginosa", rank: "species", priority: 75 },
  { taxid: 573, scientific_name: "Klebsiella pneumoniae", rank: "species", priority: 70 },
  { taxid: 1496, scientific_name: "Clostridioides difficile", common_name: "C. diff", rank: "species", priority: 70 },
  { taxid: 28901, scientific_name: "Salmonella enterica", rank: "species", priority: 70 },

  // Parasites / fungi
  { taxid: 5833, scientific_name: "Plasmodium falciparum", common_name: "malaria parasite", rank: "species", priority: 75 },
  { taxid: 5476, scientific_name: "Candida albicans", rank: "species", priority: 70 },

  // Viruses
  { taxid: 2697049, scientific_name: "Severe acute respiratory syndrome coronavirus 2", common_name: "SARS-CoV-2", rank: "isolate", synonyms: ["covid", "sarscov2"], priority: 95 },
  { taxid: 11676, scientific_name: "Human immunodeficiency virus 1", common_name: "HIV-1", rank: "species", synonyms: ["hiv1"], priority: 80 },
  { taxid: 11320, scientific_name: "Influenza A virus", common_name: "flu", rank: "species", priority: 75 },
  { taxid: 12721, scientific_name: "Hepatitis C virus", common_name: "HCV", rank: "species", priority: 65 },

  // High-level groupings (useful as broad filters)
  { taxid: 2, scientific_name: "Bacteria", rank: "superkingdom", priority: 60 },
  { taxid: 2157, scientific_name: "Archaea", rank: "superkingdom", priority: 55 },
  { taxid: 2759, scientific_name: "Eukaryota", rank: "superkingdom", priority: 55 },
  { taxid: 10239, scientific_name: "Viruses", rank: "superkingdom", priority: 60 },
  { taxid: 4751, scientific_name: "Fungi", rank: "kingdom", priority: 55 },
  { taxid: 33208, scientific_name: "Metazoa", common_name: "animals", rank: "kingdom", priority: 55 },
  { taxid: 40674, scientific_name: "Mammalia", common_name: "mammals", rank: "class", priority: 60 },
  { taxid: 9443, scientific_name: "Primates", rank: "order", priority: 55 },
  { taxid: 50557, scientific_name: "Insecta", common_name: "insects", rank: "class", priority: 50 },
  { taxid: 7742, scientific_name: "Vertebrata", common_name: "vertebrates", rank: "subphylum", priority: 55 },
];

function buildHaystack(seed: Seed): string {
  const parts: string[] = [seed.scientific_name];
  if (seed.common_name) parts.push(seed.common_name);
  if (seed.synonyms) parts.push(...seed.synonyms);
  parts.push(String(seed.taxid));
  return parts.join(" ").toLowerCase();
}

export const COMMON_TAXA: CommonTaxon[] = SEEDS.map((seed) => ({
  taxid: seed.taxid,
  scientific_name: seed.scientific_name,
  common_name: seed.common_name ?? null,
  rank: seed.rank,
  search_text: buildHaystack(seed),
  priority: seed.priority ?? 50,
}));

const COMMON_BY_TAXID = new Map<number, CommonTaxon>(
  COMMON_TAXA.map((t) => [t.taxid, t]),
);

/** Lookup by taxid; used to enrich live-search rows with the "common" badge. */
export function getCommonTaxon(taxid: number): CommonTaxon | null {
  return COMMON_BY_TAXID.get(taxid) ?? null;
}

/**
 * Case-insensitive prefix-and-substring match against the curated list.
 *
 * Ranking:
 *  1. Exact case-insensitive scientific_name match
 *  2. Prefix match on scientific_name or common_name
 *  3. Substring match anywhere in the haystack
 *  4. Numeric taxid match
 *
 * Within each tier, higher `priority` wins.
 */
export function filterCommonTaxa(rawQuery: string, limit = 8): CommonTaxon[] {
  const query = rawQuery.trim().toLowerCase();
  if (!query) return [];

  type Scored = { taxon: CommonTaxon; tier: number };
  const scored: Scored[] = [];

  for (const taxon of COMMON_TAXA) {
    const sciLower = taxon.scientific_name.toLowerCase();
    const commonLower = (taxon.common_name ?? "").toLowerCase();

    let tier = -1;
    if (sciLower === query || commonLower === query) {
      tier = 0;
    } else if (sciLower.startsWith(query) || commonLower.startsWith(query)) {
      tier = 1;
    } else if (taxon.search_text.includes(query)) {
      tier = 2;
    } else if (/^\d+$/.test(query) && String(taxon.taxid).startsWith(query)) {
      tier = 3;
    }
    if (tier >= 0) scored.push({ taxon, tier });
  }

  scored.sort((a, b) => {
    if (a.tier !== b.tier) return a.tier - b.tier;
    if (a.taxon.priority !== b.taxon.priority) {
      return b.taxon.priority - a.taxon.priority;
    }
    return a.taxon.scientific_name.localeCompare(b.taxon.scientific_name);
  });

  return scored.slice(0, limit).map((s) => s.taxon);
}

/** Top-priority taxa for "quick pick" chips when there is no query. */
export function topCommonTaxa(limit = 6): CommonTaxon[] {
  return [...COMMON_TAXA]
    .sort((a, b) => b.priority - a.priority)
    .slice(0, limit);
}
