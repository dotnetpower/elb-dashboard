"""Web BLAST parity comparator and taxonomy-exclusion verifier.

Responsibility: provide an offline, deterministic harness that compares a
candidate BLAST+ XML output against the captured NCBI Web BLAST reference XML
for the same query, and verifies that the taxonomic exclusion filter requested
in the form (`-negative_taxids` on our side, `ENTREZ_QUERY=NOT txid<N>[ORGN]`
on NCBI's side) has actually purged the excluded organism from the hit set.
This is the result-side counterpart of the request-side contract in
`api/services/blast/config.py` (`generate_config`).
Edit boundaries: keep this module side-effect-free and dependency-light. It
only reads files (plain `.xml` or `.xml.gz`) and returns plain dataclasses.
The opt-in NCBI fetcher lives in `scripts/dev/fetch-ncbi-blast-rid.py`; do
not put network calls in here.
Key entry points: `parse_summary`, `compare_summaries`, `verify_exclusion`.
Risky contracts: tolerate cross-snapshot drift when callers request it —
exact rank-set / HSP equality is only meaningful when both XMLs were
produced against the same `core_nt` database snapshot. The
`ParityReport.snapshot_drift` flag records whether the comparator detected
a `BlastOutput_db` / Statistics drift.
Validation: `uv run pytest -q api/tests/test_web_blast_parity_xml.py`.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree as ET

_ACC_RE = re.compile(r"^([A-Z]+_?\d+(?:\.\d+)?)$", re.IGNORECASE)


@dataclass(frozen=True)
class WebBlastHit:
    """One canonical hit summary, comparable across XML snapshots."""

    rank: int
    accession: str
    hit_id: str
    organism: str
    bit_score: float
    raw_score: int
    evalue: float
    identity: int
    align_len: int
    gaps: int
    query_from: int
    query_to: int
    hit_from: int
    hit_to: int

    @property
    def percent_identity(self) -> float:
        if self.align_len <= 0:
            return 0.0
        return round(self.identity * 100.0 / self.align_len, 3)


@dataclass(frozen=True)
class WebBlastSummary:
    """The subset of a BlastOutput we use for parity comparison.

    `hits` is sorted by NCBI-reported rank (Hit_num). When comparing two
    summaries we trust the rank order to match for the same query against the
    same DB snapshot; if the DB has drifted, rank order is allowed to vary.
    """

    program: str
    version: str
    database: str
    query_id: str
    query_def: str
    query_len: int
    evalue_threshold: float
    filter_string: str
    db_num: int
    db_len: int
    eff_space: int
    hits: tuple[WebBlastHit, ...]


@dataclass
class ParityReport:
    """Result of comparing a candidate XML against a reference XML."""

    equivalent: bool
    snapshot_drift: bool
    findings: list[str] = field(default_factory=list)
    reference_db_num: int = 0
    candidate_db_num: int = 0
    reference_rank_count: int = 0
    candidate_rank_count: int = 0
    rank_set_only_in_reference: list[str] = field(default_factory=list)
    rank_set_only_in_candidate: list[str] = field(default_factory=list)
    hsp_drift: list[dict] = field(default_factory=list)


def _open_xml(path: Path) -> str:
    """Read XML from a plain `.xml` file or a `.xml.gz` archive."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8")


def _local(tag: str) -> str:
    """Strip any XML namespace prefix."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _text(parent: ET.Element | None, child: str) -> str | None:
    if parent is None:
        return None
    el = parent.find(child)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _int(parent: ET.Element | None, child: str, default: int = 0) -> int:
    raw = _text(parent, child)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(parent: ET.Element | None, child: str, default: float = 0.0) -> float:
    raw = _text(parent, child)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _organism_from_def(hit_def: str) -> str:
    """Best-effort organism extraction from a Hit_def line.

    NCBI Hit_def usually has the form
    `<organism description>, <molecule type>` or `<organism>, complete genome`.
    We strip everything after the first comma and trim residual record-type
    tokens, which is good enough for substring-based exclusion checks.
    """
    cleaned = hit_def.split(",", 1)[0].strip()
    cleaned = re.sub(
        r"\s+(complete (?:genome|sequence|cds)|isolate.*|strain.*|chromosome.*|partial.*)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned


def parse_summary(path: str | Path) -> WebBlastSummary:
    """Parse a captured BLAST XML (plain or gzipped) into a `WebBlastSummary`."""
    p = Path(path)
    content = _open_xml(p)
    root = ET.fromstring(content)
    if _local(root.tag) != "BlastOutput":
        raise ValueError(f"{p}: root element is {root.tag!r}, expected <BlastOutput>")

    program = _text(root, "BlastOutput_program") or ""
    version = _text(root, "BlastOutput_version") or ""
    database = _text(root, "BlastOutput_db") or ""
    query_id = _text(root, "BlastOutput_query-ID") or ""
    query_def = _text(root, "BlastOutput_query-def") or ""
    query_len = _int(root, "BlastOutput_query-len")

    params = root.find("BlastOutput_param/Parameters")
    evalue_threshold = _float(params, "Parameters_expect")
    filter_string = _text(params, "Parameters_filter") or ""

    iteration = root.find(".//Iteration")
    stats = root.find(".//Iteration_stat/Statistics")
    db_num = _int(stats, "Statistics_db-num")
    db_len = _int(stats, "Statistics_db-len")
    eff_space = _int(stats, "Statistics_eff-space")

    hits: list[WebBlastHit] = []
    if iteration is not None:
        for hit_el in iteration.findall("Iteration_hits/Hit"):
            rank = _int(hit_el, "Hit_num")
            hit_id = _text(hit_el, "Hit_id") or ""
            accession = _text(hit_el, "Hit_accession") or ""
            hit_def = _text(hit_el, "Hit_def") or ""
            best_hsp = hit_el.find("Hit_hsps/Hsp")
            if best_hsp is None:
                continue
            hits.append(
                WebBlastHit(
                    rank=rank,
                    accession=accession,
                    hit_id=hit_id,
                    organism=_organism_from_def(hit_def),
                    bit_score=_float(best_hsp, "Hsp_bit-score"),
                    raw_score=_int(best_hsp, "Hsp_score"),
                    evalue=_float(best_hsp, "Hsp_evalue"),
                    identity=_int(best_hsp, "Hsp_identity"),
                    align_len=_int(best_hsp, "Hsp_align-len"),
                    gaps=_int(best_hsp, "Hsp_gaps"),
                    query_from=_int(best_hsp, "Hsp_query-from"),
                    query_to=_int(best_hsp, "Hsp_query-to"),
                    hit_from=_int(best_hsp, "Hsp_hit-from"),
                    hit_to=_int(best_hsp, "Hsp_hit-to"),
                )
            )
    hits.sort(key=lambda h: h.rank)
    return WebBlastSummary(
        program=program,
        version=version,
        database=database,
        query_id=query_id,
        query_def=query_def,
        query_len=query_len,
        evalue_threshold=evalue_threshold,
        filter_string=filter_string,
        db_num=db_num,
        db_len=db_len,
        eff_space=eff_space,
        hits=tuple(hits),
    )


def _accession_key(hit: WebBlastHit) -> str:
    """Canonical key for comparing hits across runs (accession w/o version)."""
    acc = hit.accession or hit.hit_id
    if acc:
        # Strip a trailing `.N` version suffix so that a newer DB snapshot's
        # bumped version (e.g. AB12345.2 vs AB12345.1) still compares equal.
        return acc.split(".", 1)[0].upper()
    return hit.organism.upper()


def compare_summaries(
    reference: WebBlastSummary,
    candidate: WebBlastSummary,
    *,
    tolerate_db_drift: bool | None = None,
    evalue_rel_tol: float = 0.01,
    bit_score_rel_tol: float = 0.005,
) -> ParityReport:
    """Compare two BLAST summaries and return a structured parity report.

    When the DB snapshots differ (`Statistics_db-num` or `Statistics_db-len`
    not equal), the comparator downgrades to set-based comparison: the
    accession sets must match exactly when `tolerate_db_drift=False`; when
    `True`, only a subset relationship is required (candidate is contained in
    the reference set within `evalue_rel_tol`).

    Default for `tolerate_db_drift`: auto -- true when DB stats differ, false
    when they match.
    """
    findings: list[str] = []
    snapshot_drift = (
        reference.db_num != candidate.db_num or reference.db_len != candidate.db_len
    )
    if tolerate_db_drift is None:
        tolerate_db_drift = snapshot_drift

    if reference.program != candidate.program:
        findings.append(
            f"program mismatch: ref={reference.program!r} cand={candidate.program!r}"
        )
    if reference.database != candidate.database:
        findings.append(
            f"database name mismatch: ref={reference.database!r} cand={candidate.database!r}"
        )
    if reference.query_len != candidate.query_len:
        findings.append(
            f"query-len mismatch: ref={reference.query_len} cand={candidate.query_len}"
        )
    if abs(reference.evalue_threshold - candidate.evalue_threshold) > 1e-9:
        findings.append(
            f"evalue threshold mismatch: ref={reference.evalue_threshold} "
            f"cand={candidate.evalue_threshold}"
        )
    if reference.filter_string.split(";", 1)[0].strip().upper() != candidate.filter_string.split(
        ";", 1
    )[0].strip().upper():
        findings.append(
            f"filter mismatch: ref={reference.filter_string!r} "
            f"cand={candidate.filter_string!r}"
        )

    ref_keys = {_accession_key(h): h for h in reference.hits}
    cand_keys = {_accession_key(h): h for h in candidate.hits}
    only_ref = sorted(set(ref_keys) - set(cand_keys))
    only_cand = sorted(set(cand_keys) - set(ref_keys))

    if not tolerate_db_drift:
        if only_ref:
            findings.append(f"{len(only_ref)} accessions present only in reference")
        if only_cand:
            findings.append(f"{len(only_cand)} accessions present only in candidate")
    else:
        # In drift mode, require that the candidate has not invented hits that
        # were not in the reference at all; the reference set is the truth.
        if only_cand:
            findings.append(
                f"{len(only_cand)} accessions in candidate not present in reference"
                " (likely DB snapshot drift)"
            )

    hsp_drift: list[dict] = []
    shared = sorted(set(ref_keys) & set(cand_keys))
    for key in shared:
        rh = ref_keys[key]
        ch = cand_keys[key]
        diffs: dict = {}
        if rh.evalue == 0.0 and ch.evalue == 0.0:
            pass
        else:
            denom = max(abs(rh.evalue), abs(ch.evalue), 1e-300)
            if abs(rh.evalue - ch.evalue) / denom > evalue_rel_tol:
                diffs["evalue"] = {"reference": rh.evalue, "candidate": ch.evalue}
        if rh.bit_score and ch.bit_score:
            denom = max(abs(rh.bit_score), abs(ch.bit_score), 1e-9)
            if abs(rh.bit_score - ch.bit_score) / denom > bit_score_rel_tol:
                diffs["bit_score"] = {
                    "reference": rh.bit_score,
                    "candidate": ch.bit_score,
                }
        if rh.identity != ch.identity or rh.align_len != ch.align_len:
            diffs["alignment"] = {
                "reference": (rh.identity, rh.align_len),
                "candidate": (ch.identity, ch.align_len),
            }
        if diffs:
            hsp_drift.append({"accession": key, **diffs})

    if hsp_drift and not tolerate_db_drift:
        findings.append(f"{len(hsp_drift)} HSPs drifted beyond tolerance")
    elif hsp_drift:
        findings.append(
            f"{len(hsp_drift)} HSPs drifted (db snapshot drift tolerated)"
        )

    equivalent = not findings
    return ParityReport(
        equivalent=equivalent,
        snapshot_drift=snapshot_drift,
        findings=findings,
        reference_db_num=reference.db_num,
        candidate_db_num=candidate.db_num,
        reference_rank_count=len(reference.hits),
        candidate_rank_count=len(candidate.hits),
        rank_set_only_in_reference=only_ref,
        rank_set_only_in_candidate=only_cand,
        hsp_drift=hsp_drift,
    )


def verify_exclusion(
    summary: WebBlastSummary,
    *,
    query_accession: str,
    excluded_markers: Iterable[str],
) -> list[str]:
    """Return a list of human-readable violations of the exclusion filter.

    Two complementary checks:

    1. The query's own source accession (the organism we were studying) must
       not appear as a hit. This catches the "exclusion filter dropped"
       failure mode regardless of how taxonomy lineages were specified.
    2. None of the captured organism marker substrings (e.g. "Plasmodium
       falciparum", "SARS-CoV-2") may appear in any `Hit_def`. This catches
       species-level leakage when the form filter used a higher-rank taxid.
    """
    violations: list[str] = []
    q_key = query_accession.split(".", 1)[0].upper()
    markers = [m for m in excluded_markers if m]
    for hit in summary.hits:
        key = _accession_key(hit)
        if key == q_key:
            violations.append(
                f"rank {hit.rank}: query source accession {hit.accession} re-hit itself"
            )
        for marker in markers:
            target = f"{hit.organism}\n{hit.hit_id}"
            if marker.lower() in target.lower():
                violations.append(
                    f"rank {hit.rank}: excluded marker {marker!r} found in "
                    f"organism/{hit.organism!r}"
                )
                break
    return violations
