import type { BlastTieCutoff } from "@/api/blast";

export interface TieCutoffNotice {
  kind: "diversity" | "exact" | "overflow";
  message: string;
}

export function tieCutoffNotice(tieCutoff: BlastTieCutoff): TieCutoffNotice {
  const {
    overflow_count,
    diversity_reserved_count,
    diversity_candidate_count,
    max_target_seqs,
  } = tieCutoff;
  const limitText = max_target_seqs ? ` (max_target_seqs=${max_target_seqs})` : "";
  if (tieCutoff.selection_equivalence === "full_db_hitlist_exact") {
    return {
      kind: "exact",
      message: `Full-DB-exact merge used BLAST raw-score and database OID ordering${limitText}; ${overflow_count} additional tied subject${
        overflow_count === 1 ? " was" : "s were"
      } outside the native hitlist window.`,
    };
  }
  if (diversity_reserved_count > 0) {
    const candidateText = diversity_candidate_count
      ? ` from ${diversity_candidate_count} lower-scoring candidate${
          diversity_candidate_count === 1 ? "" : "s"
        }`
      : "";
    return {
      kind: "diversity",
      message: `Variant-aware merge reserved ${diversity_reserved_count} slot${
        diversity_reserved_count === 1 ? "" : "s"
      }${candidateText}${limitText}. The strict cutoff excluded ${overflow_count} tied top-score hit${
        overflow_count === 1 ? "" : "s"
      }, and ${diversity_reserved_count} additional tied hit${
        diversity_reserved_count === 1 ? " was" : "s were"
      } replaced.`,
    };
  }
  return {
    kind: "overflow",
    message: `Displayed hits are a sample of a larger tied score class — ${overflow_count} hit${
      overflow_count === 1 ? "" : "s"
    } with the same top score ${overflow_count === 1 ? "was" : "were"} not shown${limitText}.`,
  };
}