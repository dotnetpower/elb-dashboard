"""BLAST database selection oracle (recommendation rule table).

Responsibility: Given a query molecule type, BLAST program, search goal, and an
optional taxonomic scope, return one recommended NCBI database plus one
alternative, each with a plain-language rationale. Pure decision logic over a
versioned static rule table — no Azure calls.
Edit boundaries: Keep the rule table and resolution logic here. Routes call
`recommend_database`; do not duplicate the rules elsewhere. Bump
`RECOMMENDATION_RULESET_VERSION` whenever the table changes.
Key entry points: `recommend_database`, `RECOMMENDATION_RULESET_VERSION`,
`SUPPORTED_GOALS`, `SUPPORTED_MOLECULES`.
Risky contracts: Output is researcher-facing guidance, not a guarantee; the
rationale must stay accurate to current NCBI database semantics.
Validation: `uv run pytest -q api/tests/test_blast_db_recommendation.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RECOMMENDATION_RULESET_VERSION = "2026-06-01"

Molecule = Literal["dna", "protein"]
SUPPORTED_MOLECULES: tuple[str, ...] = ("dna", "protein")
SUPPORTED_GOALS: tuple[str, ...] = (
    "identify",
    "highly_similar",
    "transcripts",
    "genomes",
    "well_characterized",
    "comprehensive",
)

# Map a normalised BLAST program to the effective query molecule the user is
# searching *with* (blastx submits DNA but searches protein space; the database
# is protein, so we treat its recommendation in the protein family).
_PROGRAM_TO_DB_MOLECULE: dict[str, Molecule] = {
    "blastn": "dna",
    "megablast": "dna",
    "dc-megablast": "dna",
    "tblastn": "dna",  # protein query against translated nucleotide DB
    "tblastx": "dna",
    "blastp": "protein",
    "blastx": "protein",  # translated DNA query against protein DB
    "psiblast": "protein",
    "deltablast": "protein",
    "rpsblast": "protein",  # protein/translated query against a conserved-domain (protein) DB
    "rpstblastn": "protein",  # translated nucleotide query against a protein domain DB
}


# Known NCBI BLAST databases -> the molecule type of the database itself, used
# ONLY for the best-effort submit-time program/database compatibility check
# (e.g. blastp against a nucleotide DB). Keys are the bare DB name, lower-cased.
# This is intentionally a curated allow-list of the well-known NCBI databases:
# an UNKNOWN database (a user's custom BLAST DB, or one not listed here) maps to
# ``None`` and is therefore never rejected — only an unambiguous mismatch
# between a known program and a known database is blocked. Add new NCBI
# databases here as they are surfaced in the picker.
_KNOWN_DATABASE_MOLECULE: dict[str, Molecule] = {
    # Nucleotide databases
    "core_nt": "dna",
    "nt": "dna",
    "refseq_rna": "dna",
    "refseq_select_rna": "dna",
    "refseq_representative_genomes": "dna",
    "16s_ribosomal_rna": "dna",
    "18s_fungal_sequences": "dna",
    "28s_fungal_sequences": "dna",
    "its_eukaryote_sequences": "dna",
    "its_refseq_fungi": "dna",
    "lsu_eukaryote_rrna": "dna",
    "lsu_prokaryote_rrna": "dna",
    "ssu_eukaryote_rrna": "dna",
    "betacoronavirus": "dna",
    "ref_euk_rep_genomes": "dna",
    "ref_prok_rep_genomes": "dna",
    "ref_viroids_rep_genomes": "dna",
    "ref_viruses_rep_genomes": "dna",
    "env_nt": "dna",
    "patnt": "dna",
    "pdbnt": "dna",
    "tsa_nt": "dna",
    # Protein databases
    "nr": "protein",
    "refseq_protein": "protein",
    "refseq_select_prot": "protein",
    "swissprot": "protein",
    "pdbaa": "protein",
    "landmark": "protein",
    "env_nr": "protein",
    "pataa": "protein",
    "tsa_nr": "protein",
    "cdd": "protein",
    "cdd_delta": "protein",
}


def _molecule_word(molecule: Molecule) -> str:
    return "nucleotide" if molecule == "dna" else "protein"


def program_database_compatibility_error(program: str, database: str) -> str | None:
    """Return a human-readable error if a KNOWN program×database molecule
    mismatch is detected at submit time, else ``None`` (allow).

    Best-effort and deliberately conservative: it returns ``None`` (i.e. lets
    the submit proceed) whenever EITHER side is unknown — an unrecognised
    program (a future BLAST+ addition) or an unrecognised database (a user's
    custom BLAST DB). Only an unambiguous mismatch between a known program and a
    known NCBI database (e.g. ``blastp`` against the nucleotide ``core_nt``) is
    blocked, so a custom database is never falsely rejected. The point is to
    convert a guaranteed ~30-minute pod failure into an instant, clear 4xx.
    """
    from api.services.blast.db_metadata import extract_db_name

    prog = (program or "").strip().lower()
    required = _PROGRAM_TO_DB_MOLECULE.get(prog)
    if required is None:
        return None
    db_name = extract_db_name(database or "").strip().lower()
    if not db_name:
        return None
    actual = _KNOWN_DATABASE_MOLECULE.get(db_name)
    if actual is None or actual == required:
        return None
    return (
        f"{prog} searches a {_molecule_word(required)} database, but "
        f"'{db_name}' is a {_molecule_word(actual)} database. Choose a "
        f"{_molecule_word(required)} database, or a program that searches "
        f"{_molecule_word(actual)} space."
    )


@dataclass(frozen=True)
class DatabaseSuggestion:
    db: str
    label: str
    rationale: str


@dataclass(frozen=True)
class Recommendation:
    ruleset_version: str
    molecule: Molecule
    goal: str
    program: str
    taxon: str | None
    recommended: DatabaseSuggestion
    alternative: DatabaseSuggestion
    notes: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "ruleset_version": self.ruleset_version,
            "molecule": self.molecule,
            "goal": self.goal,
            "program": self.program,
            "taxon": self.taxon,
            "recommended": {
                "db": self.recommended.db,
                "label": self.recommended.label,
                "rationale": self.recommended.rationale,
            },
            "alternative": {
                "db": self.alternative.db,
                "label": self.alternative.label,
                "rationale": self.alternative.rationale,
            },
            "notes": list(self.notes),
        }


# (molecule, goal) -> (recommended, alternative)
_RULES: dict[tuple[Molecule, str], tuple[DatabaseSuggestion, DatabaseSuggestion]] = {
    ("dna", "identify"): (
        DatabaseSuggestion(
            "core_nt",
            "Core nucleotide (core_nt)",
            "Broad GenBank+EMBL+DDBJ+RefSeq coverage; the default choice for "
            "identifying an unknown nucleotide sequence.",
        ),
        DatabaseSuggestion(
            "nt",
            "Nucleotide collection (nt)",
            "Larger, less curated superset of core_nt — use when core_nt misses a "
            "rare or very recent sequence, at the cost of more redundant hits.",
        ),
    ),
    ("dna", "highly_similar"): (
        DatabaseSuggestion(
            "core_nt",
            "Core nucleotide (core_nt)",
            "Pair with megablast for near-identical matches; core_nt gives clean, "
            "low-redundancy top hits.",
        ),
        DatabaseSuggestion(
            "ref_euk_rep_genomes",
            "RefSeq representative eukaryotic genomes",
            "Use when you expect the match to be genomic rather than a deposited "
            "GenBank record.",
        ),
    ),
    ("dna", "transcripts"): (
        DatabaseSuggestion(
            "refseq_rna",
            "RefSeq RNA (refseq_rna)",
            "Curated, non-redundant transcript set — ideal for matching mRNA / "
            "transcript sequences to known genes.",
        ),
        DatabaseSuggestion(
            "core_nt",
            "Core nucleotide (core_nt)",
            "Fall back to core_nt when the transcript may not yet be in RefSeq.",
        ),
    ),
    ("dna", "genomes"): (
        DatabaseSuggestion(
            "ref_prok_rep_genomes",
            "RefSeq representative prokaryotic genomes",
            "Representative assemblies keep the search space small for prokaryotic "
            "genome-scale matching.",
        ),
        DatabaseSuggestion(
            "ref_euk_rep_genomes",
            "RefSeq representative eukaryotic genomes",
            "Switch to the eukaryotic representative set when the organism is a "
            "eukaryote.",
        ),
    ),
    ("dna", "well_characterized"): (
        DatabaseSuggestion(
            "refseq_rna",
            "RefSeq RNA (refseq_rna)",
            "RefSeq is curated, so hits come with stable, well-annotated records.",
        ),
        DatabaseSuggestion(
            "core_nt",
            "Core nucleotide (core_nt)",
            "Broaden to core_nt if RefSeq lacks the lineage you need.",
        ),
    ),
    ("dna", "comprehensive"): (
        DatabaseSuggestion(
            "nt",
            "Nucleotide collection (nt)",
            "The most complete nucleotide collection — maximises sensitivity when "
            "you cannot miss any deposited sequence.",
        ),
        DatabaseSuggestion(
            "core_nt",
            "Core nucleotide (core_nt)",
            "Prefer core_nt first for a faster, less redundant search; escalate to "
            "nt only if needed.",
        ),
    ),
    ("protein", "identify"): (
        DatabaseSuggestion(
            "nr",
            "Non-redundant protein (nr)",
            "The standard broad protein database for identifying an unknown "
            "protein or translated query.",
        ),
        DatabaseSuggestion(
            "refseq_protein",
            "RefSeq protein (refseq_protein)",
            "Curated alternative when you want annotated RefSeq records rather than "
            "the full nr redundancy.",
        ),
    ),
    ("protein", "well_characterized"): (
        DatabaseSuggestion(
            "swissprot",
            "UniProtKB/Swiss-Prot (swissprot)",
            "Manually reviewed, richly annotated proteins — best when functional "
            "annotation quality matters more than coverage.",
        ),
        DatabaseSuggestion(
            "refseq_protein",
            "RefSeq protein (refseq_protein)",
            "Broader than Swiss-Prot while still curated.",
        ),
    ),
    ("protein", "comprehensive"): (
        DatabaseSuggestion(
            "nr",
            "Non-redundant protein (nr)",
            "Maximum protein coverage for the most sensitive search.",
        ),
        DatabaseSuggestion(
            "refseq_protein",
            "RefSeq protein (refseq_protein)",
            "Use when you prefer curated RefSeq records over full nr redundancy.",
        ),
    ),
}

# Goals that only make sense for one molecule fall back to a sibling goal.
_GOAL_FALLBACK: dict[tuple[Molecule, str], str] = {
    ("protein", "highly_similar"): "identify",
    ("protein", "transcripts"): "identify",
    ("protein", "genomes"): "comprehensive",
    ("dna", "well_characterized"): "well_characterized",
}


def _normalise_molecule(molecule: str | None, program: str | None) -> Molecule:
    prog = (program or "").strip().lower()
    if prog in _PROGRAM_TO_DB_MOLECULE:
        return _PROGRAM_TO_DB_MOLECULE[prog]
    mol = (molecule or "").strip().lower()
    if mol in {"protein", "prot", "aa", "amino"}:
        return "protein"
    return "dna"


def recommend_database(
    *,
    molecule: str | None = None,
    program: str | None = None,
    goal: str | None = None,
    taxon: str | None = None,
) -> Recommendation:
    """Return a recommended + alternative database for the described search."""
    resolved_molecule = _normalise_molecule(molecule, program)
    resolved_goal = (goal or "identify").strip().lower()
    if resolved_goal not in SUPPORTED_GOALS:
        resolved_goal = "identify"

    key = (resolved_molecule, resolved_goal)
    if key not in _RULES:
        fallback_goal = _GOAL_FALLBACK.get(key, "identify")
        key = (resolved_molecule, fallback_goal)
        if key not in _RULES:
            key = (resolved_molecule, "identify")
        resolved_goal = key[1]

    recommended, alternative = _RULES[key]

    notes: list[str] = []
    taxon_clean = (taxon or "").strip()
    if taxon_clean:
        notes.append(
            f"Restrict to taxon '{taxon_clean}' with an organism / taxid filter "
            "(-taxids) rather than switching databases; the recommendation above "
            "already covers this lineage."
        )
    notes.append(
        "Databases run from your own warmed AKS + Storage snapshot, so the choice "
        "only affects search space and runtime — never a shared queue."
    )

    return Recommendation(
        ruleset_version=RECOMMENDATION_RULESET_VERSION,
        molecule=resolved_molecule,
        goal=resolved_goal,
        program=(program or "").strip().lower(),
        taxon=taxon_clean or None,
        recommended=recommended,
        alternative=alternative,
        notes=notes,
    )
