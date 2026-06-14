"""BLAST run citation / Methods-paragraph construction.

Responsibility: Render a reproducible Methods paragraph, Markdown, and BibTeX for
a completed BLAST job from its provenance bundle alone (no extra Azure calls).
Edit boundaries: Keep reusable domain logic here; routes and tasks call this layer
instead of duplicating string templating. No SDK or HTTP work in this module.
Key entry points: `build_citation`, `CitationBundle`, `CITATION_FORMATS`.
Risky contracts: Input is the persisted `provenance` bundle from
`build_blast_provenance`; never emit Storage URLs or SAS tokens. Output strings are
sanitised plain text safe for clipboard copy.
Validation: `uv run pytest -q api/tests/test_blast_citation.py`.
"""

# This module embeds literal BibTeX citation records whose author lists exceed
# the 100-column line limit; E501 is intentionally disabled file-wide.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

CITATION_FORMATS = ("text", "markdown", "bibtex")

# Canonical references for the tools the control plane invokes. These are stable,
# peer-reviewed citations and do not depend on the individual job.
_BLAST_PLUS_REFERENCE = (
    "Camacho C, Coulouris G, Avagyan V, Ma N, Papadopoulos J, Bealer K, "
    "Madden TL. BLAST+: architecture and applications. "
    "BMC Bioinformatics. 2009;10:421."
)
_ELASTIC_BLAST_REFERENCE = (
    "Boratyn GM, Camacho C, Cooper PS, Coulouris G, Fong A, Ma N, Madden TL, "
    "Matten WT, McGinnis SD, Merezhuk Y, et al. ElasticBLAST: accelerating "
    "sequence search via cloud computing. BMC Bioinformatics. 2023;24:117."
)

_BLAST_PLUS_BIBTEX = """@article{camacho2009blast,
  title   = {{BLAST+}: architecture and applications},
  author  = {Camacho, Christiam and Coulouris, George and Avagyan, Vahram and Ma, Ning and Papadopoulos, Jason and Bealer, Kevin and Madden, Thomas L.},
  journal = {BMC Bioinformatics},
  volume  = {10},
  pages   = {421},
  year    = {2009},
  doi     = {10.1186/1471-2105-10-421}
}"""

_ELASTIC_BLAST_BIBTEX = """@article{boratyn2023elasticblast,
  title   = {ElasticBLAST: accelerating sequence search via cloud computing},
  author  = {Boratyn, Greg M. and Camacho, Christiam and Cooper, Peter S. and Coulouris, George and Fong, Amelia and Ma, Ning and Madden, Thomas L. and Matten, Wayne T. and McGinnis, Scott D. and Merezhuk, Yuri and others},
  journal = {BMC Bioinformatics},
  volume  = {24},
  pages   = {117},
  year    = {2023},
  doi     = {10.1186/s12859-023-05245-9}
}"""


@dataclass(frozen=True)
class CitationBundle:
    """Rendered citation in every supported format plus the structured fields."""

    job_id: str
    rid: str
    program: str
    blast_version: str
    database: str
    database_snapshot: str | None
    search_space: str | None
    text: str
    markdown: str
    bibtex: str

    def render(self, fmt: str) -> str:
        if fmt == "markdown":
            return self.markdown
        if fmt == "bibtex":
            return self.bibtex
        return self.text


def _short_rid(job_id: str) -> str:
    """Return a synthetic, clearly non-NCBI request id for the run."""
    cleaned = (job_id or "").strip()
    if not cleaned:
        return "ELB-unknown"
    # Keep it readable but bounded; never imply an NCBI-issued RID.
    token = cleaned.split("/")[-1]
    return f"ELB-{token}"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _format_options(options: Mapping[str, Any]) -> str:
    """Render a compact, deterministic option clause for the Methods sentence."""
    interesting = (
        ("evalue", "an expect threshold of"),
        ("matrix", "the"),
        ("max_target_seqs", "at most"),
        ("word_size", "a word size of"),
        ("gap_open", "a gap-open penalty of"),
        ("gap_extend", "a gap-extend penalty of"),
    )
    parts: list[str] = []
    for key, lead in interesting:
        raw = options.get(key)
        if raw in (None, ""):
            continue
        if key == "matrix":
            parts.append(f"the {raw} scoring matrix")
        elif key == "max_target_seqs":
            parts.append(f"at most {raw} target sequences")
        elif key == "evalue":
            parts.append(f"an expect threshold of {raw}")
        else:
            parts.append(f"{lead} {raw}")
    if not parts:
        return "default search parameters"
    return ", ".join(parts)


def build_citation(
    *,
    job_id: str,
    provenance: Mapping[str, Any] | None,
    job_title: str | None = None,
    accessed: datetime | None = None,
) -> CitationBundle:
    """Build a Methods paragraph + Markdown + BibTeX from a provenance bundle.

    The provenance bundle is the dict produced by ``build_blast_provenance`` and
    persisted at ``{job_id}/provenance.json``. When fields are missing (older
    jobs), the renderer degrades to neutral wording rather than failing.
    """
    bundle = _as_mapping(provenance)
    blast = _as_mapping(bundle.get("blast"))
    database = _as_mapping(bundle.get("database"))
    options = _as_mapping(bundle.get("options"))

    program = str(blast.get("program") or "blastn")
    version = str(blast.get("version") or "unknown")
    raw_db_name = str(database.get("name") or database.get("input") or "").strip()
    have_db_name = bool(raw_db_name)
    db_name = raw_db_name or "the selected database"
    # Render "queried the {name} database" only when an actual database
    # identifier is present; otherwise emit "queried the selected database"
    # once so the sentence does not collapse to the
    # "queried the the selected database database" duplicate observed in #8.
    if have_db_name:
        text_db_phrase = f"the {db_name} database"
        markdown_db_phrase = f"the **{db_name}** database"
    else:
        text_db_phrase = "the selected database"
        markdown_db_phrase = "the selected database"
    snapshot = database.get("snapshot")
    snapshot_str = str(snapshot) if snapshot not in (None, "") else None
    search_space = database.get("search_space")
    search_space_str = str(search_space) if search_space not in (None, "") else None

    rid = _short_rid(job_id)
    accessed_dt = (accessed or datetime.now(UTC)).date().isoformat()
    title_clause = f" (job title: {job_title})" if job_title else ""

    snapshot_clause = (
        f" (database snapshot {snapshot_str})" if snapshot_str else ""
    )
    search_space_clause = (
        f" The effective search space was {search_space_str}." if search_space_str else ""
    )
    options_clause = _format_options(options)

    text = (
        f"Sequence similarity searches were performed with NCBI BLAST+ "
        f"({program}, version {version}) executed through ElasticBLAST on a "
        f"self-managed Azure Kubernetes Service cluster via the elb-dashboard "
        f"control plane (run {rid}{title_clause}). Searches queried "
        f"{text_db_phrase}{snapshot_clause} using {options_clause}."
        f"{search_space_clause} "
        f"References: {_BLAST_PLUS_REFERENCE} {_ELASTIC_BLAST_REFERENCE}"
    )

    markdown = (
        f"Sequence similarity searches were performed with **NCBI BLAST+** "
        f"(`{program}`, version `{version}`) executed through **ElasticBLAST** on a "
        f"self-managed Azure Kubernetes Service cluster via the `elb-dashboard` "
        f"control plane (run `{rid}`{title_clause}). Searches queried "
        f"{markdown_db_phrase}{snapshot_clause} using {options_clause}."
        f"{search_space_clause}\n\n"
        f"**References**\n\n"
        f"1. {_BLAST_PLUS_REFERENCE}\n"
        f"2. {_ELASTIC_BLAST_REFERENCE}\n"
    )

    run_bibtex = (
        "@misc{elbdashboard_run_" + _bibtex_key(job_id) + ",\n"
        f"  title        = {{BLAST search run {rid}}},\n"
        f"  howpublished = {{elb-dashboard control plane, program {program}, database {db_name}}},\n"
        f"  note         = {{BLAST+ version {version}"
        + (f", database snapshot {snapshot_str}" if snapshot_str else "")
        + "}},\n"
        f"  year         = {{{accessed_dt[:4]}}}\n"
        "}"
    )
    bibtex = "\n\n".join([_BLAST_PLUS_BIBTEX, _ELASTIC_BLAST_BIBTEX, run_bibtex])

    return CitationBundle(
        job_id=job_id,
        rid=rid,
        program=program,
        blast_version=version,
        database=db_name,
        database_snapshot=snapshot_str,
        search_space=search_space_str,
        text=text,
        markdown=markdown,
        bibtex=bibtex,
    )


def _bibtex_key(job_id: str) -> str:
    cleaned = "".join(ch for ch in (job_id or "") if ch.isalnum())
    return cleaned or "unknown"
