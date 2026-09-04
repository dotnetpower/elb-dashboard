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
Key entry points: `parse_summary`, `parse_xml2_deflines`, `parse_xml2_statistics`,
`compare_summaries`, `verify_exclusion`, `verify_xml2_taxid_exclusion`.
Risky contracts: `exact_equivalent` is true only for the same database
statistics, BLAST version/options, subject order, every HSP field, and search
statistics. Cross-snapshot containment is diagnostic only and must never close
an exact-parity acceptance criterion. XML2 taxonomy checks require an
authoritative ancestor-plus-descendant taxid closure supplied by the caller;
this offline module never guesses taxonomy lineage from titles. XML1 database
length wrapping is normalized only when the caller explicitly supplies an
independent authoritative snapshot match.
Validation: `uv run pytest -q api/tests/test_web_blast_parity_xml.py`.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Literal
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as ET

from api.services.blast.snapshot_drift import assess_snapshot_drift

_XML2_MAX_MEMBERS = 16
_XML2_MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class WebBlastHsp:
    """One complete canonical HSP from BLAST XML."""

    bit_score: float
    raw_score: int
    evalue: float
    query_from: int
    query_to: int
    hit_from: int
    hit_to: int
    query_frame: int
    hit_frame: int
    identity: int
    positive: int
    gaps: int
    align_len: int
    qseq: str
    hseq: str
    midline: str


@dataclass(frozen=True)
class WebBlastHit:
    """One canonical subject plus compatibility aliases for its first HSP."""

    rank: int
    accession: str
    hit_id: str
    subject_id: str
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
    hit_def: str = ""
    hit_len: int = 0
    hsps: tuple[WebBlastHsp, ...] = ()

    @property
    def percent_identity(self) -> float:
        if self.align_len <= 0:
            return 0.0
        return round(self.identity * 100.0 / self.align_len, 3)


@dataclass(frozen=True)
class WebBlastDefline:
    """One taxid-bearing descriptor from an NCBI BLAST XML2 hit group."""

    hit_rank: int
    descriptor_index: int
    accession: str
    identifier: str
    taxid: int | None
    scientific_name: str
    title: str


@dataclass(frozen=True)
class WebBlastXml2Statistics:
    """Authoritative per-query statistics exposed by NCBI BLAST XML2."""

    db_num: int
    db_len: int
    hsp_len: int
    eff_space: int
    kappa: float
    lambda_value: float
    entropy: float


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
    hsp_len: int
    eff_space: int
    kappa: float
    lambda_value: float
    entropy: float
    parameters: tuple[tuple[str, str], ...]
    hits: tuple[WebBlastHit, ...]


@dataclass
class ParityReport:
    """Result of comparing a candidate XML against a reference XML."""

    equivalent: bool
    snapshot_drift: bool
    db_len_representation_normalized: bool = False
    exact_equivalent: bool = False
    drift_compatible: bool = False
    comparison_mode: Literal["strict_exact", "drift_tolerant_containment"] = "strict_exact"
    findings: list[str] = field(default_factory=list)
    exact_findings: list[str] = field(default_factory=list)
    reference_db_num: int = 0
    candidate_db_num: int = 0
    reference_rank_count: int = 0
    candidate_rank_count: int = 0
    rank_set_only_in_reference: list[str] = field(default_factory=list)
    rank_set_only_in_candidate: list[str] = field(default_factory=list)
    hsp_drift: list[dict[str, object]] = field(default_factory=list)
    # Structured, quantified drift of the candidate run's observed database
    # statistics against the verified NCBI Web BLAST calibration (see
    # `api/services/blast/snapshot_drift.assess_snapshot_drift`). `None` when
    # the candidate did not report a database name to look up. The boolean
    # `snapshot_drift` above remains the reference-vs-candidate comparison.
    snapshot_drift_detail: dict[str, object] | None = None


def _open_xml(path: Path) -> str:
    """Read XML from a plain `.xml` file or a `.xml.gz` archive."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8")


def _open_xml2_documents(path: Path) -> tuple[bytes, ...]:
    """Read bounded XML2 documents from a plain, gzip, or NCBI ZIP payload."""
    if path.stat().st_size > _XML2_MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"{path}: XML2 payload exceeds 128 MiB")
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as fh:
            payload = fh.read(_XML2_MAX_UNCOMPRESSED_BYTES + 1)
    else:
        payload = path.read_bytes()
    if len(payload) > _XML2_MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"{path}: XML2 payload exceeds 128 MiB")
    if not payload.startswith(b"PK\x03\x04"):
        return (payload,)

    try:
        with ZipFile(BytesIO(payload)) as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".xml")
            ]
            if not members:
                raise ValueError(f"{path}: XML2 ZIP contains no XML members")
            if len(members) > _XML2_MAX_MEMBERS:
                raise ValueError(
                    f"{path}: XML2 ZIP contains {len(members)} XML members; "
                    f"maximum is {_XML2_MAX_MEMBERS}"
                )
            total_size = sum(member.file_size for member in members)
            if total_size > _XML2_MAX_UNCOMPRESSED_BYTES:
                raise ValueError(f"{path}: XML2 ZIP expands beyond 128 MiB")
            documents = tuple(archive.read(member) for member in members)
            # NCBI's ZIP includes a tiny XInclude wrapper and the actual
            # BlastOutput2 member. Some live wrappers are malformed (missing
            # whitespace between namespace attributes), so select the
            # self-contained result document rather than parsing the wrapper.
            result_documents = tuple(
                document for document in documents if b"<BlastOutput2" in document
            )
            if not result_documents:
                raise ValueError(f"{path}: XML2 ZIP contains no BlastOutput2 result")
            return result_documents
    except BadZipFile as exc:
        raise ValueError(f"{path}: invalid XML2 ZIP payload") from exc


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


def _local_text(parent: ET.Element, child_name: str) -> str | None:
    """Return one direct child's text while ignoring an XML namespace."""
    for child in list(parent):
        if _local(child.tag) == child_name:
            value = (child.text or "").strip()
            return value or None
    return None


def _local_int(parent: ET.Element, child_name: str, default: int = 0) -> int:
    value = _local_text(parent, child_name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _local_float(parent: ET.Element, child_name: str, default: float = 0.0) -> float:
    value = _local_text(parent, child_name)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _parse_xml2_roots(path: Path) -> tuple[ET.Element, ...]:
    roots: list[ET.Element] = []
    for document in _open_xml2_documents(path):
        try:
            root = ET.fromstring(document)
        except ET.ParseError as exc:
            raise ValueError(f"{path}: invalid XML2 document") from exc
        if _local(root.tag) not in {"BlastXML2", "BlastOutput2"}:
            raise ValueError(f"{path}: root element is {root.tag!r}, expected BLAST XML2")
        roots.append(root)
    if not roots:
        raise ValueError(f"{path}: no BLAST XML2 document found")
    return tuple(roots)


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


def _canonical_parameter(child: ET.Element) -> tuple[str, str]:
    name = _local(child.tag)
    value = (child.text or "").strip()
    if name == "Parameters_filter":
        return name, value.split(";", 1)[0].strip().upper()
    try:
        number = Decimal(value)
    except InvalidOperation:
        return name, value
    if number.is_zero():
        return name, "0"
    return name, format(number.normalize(), "E")


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


def _parse_hsp(hsp: ET.Element) -> WebBlastHsp:
    return WebBlastHsp(
        bit_score=_float(hsp, "Hsp_bit-score"),
        raw_score=_int(hsp, "Hsp_score"),
        evalue=_float(hsp, "Hsp_evalue"),
        query_from=_int(hsp, "Hsp_query-from"),
        query_to=_int(hsp, "Hsp_query-to"),
        hit_from=_int(hsp, "Hsp_hit-from"),
        hit_to=_int(hsp, "Hsp_hit-to"),
        query_frame=_int(hsp, "Hsp_query-frame"),
        hit_frame=_int(hsp, "Hsp_hit-frame"),
        identity=_int(hsp, "Hsp_identity"),
        positive=_int(hsp, "Hsp_positive"),
        gaps=_int(hsp, "Hsp_gaps"),
        align_len=_int(hsp, "Hsp_align-len"),
        qseq=_text(hsp, "Hsp_qseq") or "",
        hseq=_text(hsp, "Hsp_hseq") or "",
        midline=_text(hsp, "Hsp_midline") or "",
    )


def _versioned_accession(hit_id: str, accession: str) -> str:
    for pattern in (
        r"\|([A-Z]{1,4}_?\d+(?:\.\d+)?)\|?$",
        r"\|([A-Z]{1,4}_?\d+\.\d+)\|",
    ):
        match = re.search(pattern, hit_id, re.IGNORECASE)
        if match:
            return match.group(1)
    return accession or hit_id


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
    parameters = tuple(
        sorted(_canonical_parameter(child) for child in list(params)) if params is not None else ()
    )

    iterations = root.findall("./BlastOutput_iterations/Iteration")
    if len(iterations) != 1:
        raise ValueError(f"{p}: expected exactly one query iteration, found {len(iterations)}")
    iteration = iterations[0]
    stats = iteration.find("./Iteration_stat/Statistics")
    db_num = _int(stats, "Statistics_db-num")
    db_len = _int(stats, "Statistics_db-len")
    hsp_len = _int(stats, "Statistics_hsp-len")
    eff_space = _int(stats, "Statistics_eff-space")

    hits: list[WebBlastHit] = []
    for hit_el in iteration.findall("Iteration_hits/Hit"):
        rank = _int(hit_el, "Hit_num")
        hit_id = _text(hit_el, "Hit_id") or ""
        accession = _text(hit_el, "Hit_accession") or ""
        hit_def = _text(hit_el, "Hit_def") or ""
        hsps = tuple(_parse_hsp(hsp) for hsp in hit_el.findall("Hit_hsps/Hsp"))
        if not hsps:
            continue
        best_hsp = hsps[0]
        hits.append(
            WebBlastHit(
                rank=rank,
                accession=accession,
                hit_id=hit_id,
                subject_id=_versioned_accession(hit_id, accession),
                organism=_organism_from_def(hit_def),
                bit_score=best_hsp.bit_score,
                raw_score=best_hsp.raw_score,
                evalue=best_hsp.evalue,
                identity=best_hsp.identity,
                align_len=best_hsp.align_len,
                gaps=best_hsp.gaps,
                query_from=best_hsp.query_from,
                query_to=best_hsp.query_to,
                hit_from=best_hsp.hit_from,
                hit_to=best_hsp.hit_to,
                hit_def=hit_def,
                hit_len=_int(hit_el, "Hit_len"),
                hsps=hsps,
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
        hsp_len=hsp_len,
        eff_space=eff_space,
        kappa=_float(stats, "Statistics_kappa"),
        lambda_value=_float(stats, "Statistics_lambda"),
        entropy=_float(stats, "Statistics_entropy"),
        parameters=parameters,
        hits=tuple(hits),
    )


def parse_xml2_deflines(path: str | Path) -> tuple[WebBlastDefline, ...]:
    """Parse every taxid-bearing descriptor from NCBI XML2 or its ZIP envelope.

    NCBI returns XML2 as a ZIP containing an XInclude wrapper plus one result
    document. A single result hit may contain several ``HitDescr`` records for
    identical sequences. Every descriptor is retained because checking only the
    first title can miss an excluded descendant hidden in the same hit group.
    """
    source = Path(path)
    deflines: list[WebBlastDefline] = []
    for root in _parse_xml2_roots(source):
        for hit in (node for node in root.iter() if _local(node.tag) == "Hit"):
            rank_raw = _local_text(hit, "num") or "0"
            try:
                hit_rank = int(rank_raw)
            except ValueError:
                hit_rank = 0
            descriptors = [
                node for node in hit.iter() if _local(node.tag) == "HitDescr"
            ]
            for descriptor_index, descriptor in enumerate(descriptors, start=1):
                taxid_raw = _local_text(descriptor, "taxid")
                try:
                    taxid = int(taxid_raw) if taxid_raw is not None else None
                except ValueError:
                    taxid = None
                if taxid is not None and taxid <= 0:
                    taxid = None
                deflines.append(
                    WebBlastDefline(
                        hit_rank=hit_rank,
                        descriptor_index=descriptor_index,
                        accession=_local_text(descriptor, "accession") or "",
                        identifier=_local_text(descriptor, "id") or "",
                        taxid=taxid,
                        scientific_name=_local_text(descriptor, "sciname") or "",
                        title=_local_text(descriptor, "title") or "",
                    )
                )
    return tuple(deflines)


def parse_xml2_statistics(path: str | Path) -> WebBlastXml2Statistics:
    """Parse one query's complete statistics from an XML2 result.

    XML1 returned by the public Web BLAST API can zero ``hsp-len`` and
    ``eff-space`` even though XML2 for the same RID carries both. This parser
    exposes that companion evidence without conflating XML2's database fields
    with XML1's filtered/wrapped representation.
    """
    source = Path(path)
    statistics = [
        node
        for root in _parse_xml2_roots(source)
        for node in root.iter()
        if _local(node.tag) == "Statistics"
    ]
    if len(statistics) != 1:
        raise ValueError(
            f"{source}: expected exactly one XML2 Statistics block, found {len(statistics)}"
        )
    node = statistics[0]
    return WebBlastXml2Statistics(
        db_num=_local_int(node, "db-num"),
        db_len=_local_int(node, "db-len"),
        hsp_len=_local_int(node, "hsp-len"),
        eff_space=_local_int(node, "eff-space"),
        kappa=_local_float(node, "kappa"),
        lambda_value=_local_float(node, "lambda"),
        entropy=_local_float(node, "entropy"),
    )


def _accession_key(hit: WebBlastHit) -> str:
    """Canonical key for comparing hits across runs (accession w/o version)."""
    acc = hit.subject_id or hit.accession or hit.hit_id
    if acc:
        # Strip a trailing `.N` version suffix so that a newer DB snapshot's
        # bumped version (e.g. AB12345.2 vs AB12345.1) still compares equal.
        return acc.split(".", 1)[0].upper()
    return hit.organism.upper()


def _normalized_database_name(value: str) -> str:
    leaf = value.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"_shard_\d+$", "", leaf, flags=re.IGNORECASE)


def _duplicate_accession_keys(hits: tuple[WebBlastHit, ...]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for hit in hits:
        key = _accession_key(hit)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return sorted(duplicates)


def _relative_difference(left: float, right: float, tolerance: float) -> bool:
    if left == right:
        return False
    denominator = max(abs(left), abs(right), 1e-300)
    return abs(left - right) / denominator > tolerance


def _database_statistics_match(
    reference: WebBlastSummary,
    candidate: WebBlastSummary,
    *,
    authoritative_snapshot_match: bool,
) -> tuple[bool, bool]:
    """Return ``(match, wrapped_length_normalized)`` for result DB stats.

    NCBI's public XML1 renderer can emit a filtered database length modulo
    ``2**32`` while same-RID XML2 and local BLAST+ retain the full integer.
    Matching the modulo value is representation-only only after an independent
    release/count proof establishes that both searches use one snapshot.
    Sequence counts must still match exactly.
    """
    if reference.db_num != candidate.db_num:
        return False, False
    if reference.db_len == candidate.db_len:
        return True, False
    if not authoritative_snapshot_match:
        return False, False
    larger = max(reference.db_len, candidate.db_len)
    smaller = min(reference.db_len, candidate.db_len)
    if larger > 0xFFFFFFFF and larger % (2**32) == smaller:
        return True, True
    return False, False


def _hsp_differences(
    reference: WebBlastHsp,
    candidate: WebBlastHsp,
    *,
    evalue_rel_tol: float,
    bit_score_rel_tol: float,
) -> dict[str, object]:
    differences: dict[str, object] = {}
    if _relative_difference(reference.evalue, candidate.evalue, evalue_rel_tol):
        differences["evalue"] = {
            "reference": reference.evalue,
            "candidate": candidate.evalue,
        }
    if _relative_difference(reference.bit_score, candidate.bit_score, bit_score_rel_tol):
        differences["bit_score"] = {
            "reference": reference.bit_score,
            "candidate": candidate.bit_score,
        }
    exact_fields = (
        "raw_score",
        "query_from",
        "query_to",
        "hit_from",
        "hit_to",
        "query_frame",
        "hit_frame",
        "identity",
        "positive",
        "gaps",
        "align_len",
        "qseq",
        "hseq",
        "midline",
    )
    for field_name in exact_fields:
        left = getattr(reference, field_name)
        right = getattr(candidate, field_name)
        if left != right:
            differences[field_name] = {"reference": left, "candidate": right}
    return differences


def compare_summaries(
    reference: WebBlastSummary,
    candidate: WebBlastSummary,
    *,
    tolerate_db_drift: bool | None = None,
    evalue_rel_tol: float = 0.0,
    bit_score_rel_tol: float = 0.0,
    authoritative_snapshot_match: bool = False,
) -> ParityReport:
    """Compare two BLAST summaries and return a structured parity report.

    Strict equality requires matching database statistics, request metadata,
    accession sets, and primary-HSP values. Drift-tolerant diagnostics require
    only request metadata plus non-empty candidate accession containment in the
    reference set; HSP drift remains visible in ``hsp_drift`` but does not fail
    that deliberately weaker diagnostic mode.

    Default for `tolerate_db_drift`: auto -- true when DB stats differ, false
    when they match. ``equivalent`` reports the selected mode;
    ``exact_equivalent`` is always the strict same-snapshot verdict and is the
    only field suitable for a full parity claim.
    """
    common_findings: list[str] = []
    database_statistics_match, db_len_representation_normalized = (
        _database_statistics_match(
            reference,
            candidate,
            authoritative_snapshot_match=authoritative_snapshot_match,
        )
    )
    snapshot_drift = not database_statistics_match
    if tolerate_db_drift is None:
        tolerate_db_drift = snapshot_drift

    if reference.program != candidate.program:
        common_findings.append(
            f"program mismatch: ref={reference.program!r} cand={candidate.program!r}"
        )
    if _normalized_database_name(reference.database) != _normalized_database_name(
        candidate.database
    ):
        common_findings.append(
            f"database name mismatch: ref={reference.database!r} cand={candidate.database!r}"
        )
    if reference.query_def != candidate.query_def:
        common_findings.append(
            f"query definition mismatch: ref={reference.query_def!r} cand={candidate.query_def!r}"
        )
    if reference.query_len != candidate.query_len:
        common_findings.append(
            f"query-len mismatch: ref={reference.query_len} cand={candidate.query_len}"
        )
    if abs(reference.evalue_threshold - candidate.evalue_threshold) > 1e-9:
        common_findings.append(
            f"evalue threshold mismatch: ref={reference.evalue_threshold} "
            f"cand={candidate.evalue_threshold}"
        )
    if (
        reference.filter_string.split(";", 1)[0].strip().upper()
        != candidate.filter_string.split(";", 1)[0].strip().upper()
    ):
        common_findings.append(
            f"filter mismatch: ref={reference.filter_string!r} cand={candidate.filter_string!r}"
        )
    if reference.version != candidate.version:
        common_findings.append(
            f"BLAST version mismatch: ref={reference.version!r} cand={candidate.version!r}"
        )
    if reference.parameters != candidate.parameters:
        common_findings.append("BLAST parameter block mismatch")

    ref_keys = {_accession_key(h): h for h in reference.hits}
    cand_keys = {_accession_key(h): h for h in candidate.hits}
    ref_duplicates = _duplicate_accession_keys(reference.hits)
    cand_duplicates = _duplicate_accession_keys(candidate.hits)
    if ref_duplicates:
        common_findings.append(f"reference contains {len(ref_duplicates)} duplicate accession keys")
    if cand_duplicates:
        common_findings.append(
            f"candidate contains {len(cand_duplicates)} duplicate accession keys"
        )
    only_ref = sorted(set(ref_keys) - set(cand_keys))
    only_cand = sorted(set(cand_keys) - set(ref_keys))

    exact_findings = list(common_findings)
    if snapshot_drift:
        exact_findings.append(
            "database snapshot mismatch: "
            f"ref=({reference.db_num}, {reference.db_len}) "
            f"cand=({candidate.db_num}, {candidate.db_len})"
        )
    if reference.eff_space != candidate.eff_space:
        exact_findings.append(
            f"effective search space mismatch: ref={reference.eff_space} cand={candidate.eff_space}"
        )
    for field_name in ("hsp_len", "kappa", "lambda_value", "entropy"):
        left = getattr(reference, field_name)
        right = getattr(candidate, field_name)
        if left != right:
            exact_findings.append(f"{field_name} statistic mismatch: ref={left} cand={right}")
    if only_ref:
        exact_findings.append(f"{len(only_ref)} accessions present only in reference")
    if only_cand:
        exact_findings.append(f"{len(only_cand)} accessions present only in candidate")

    drift_findings = list(common_findings)
    if reference.hits and not candidate.hits:
        drift_findings.append("candidate contains no hits while reference contains hits")
    if only_cand:
        drift_findings.append(
            f"{len(only_cand)} accessions in candidate not present in reference"
            " (likely DB snapshot drift)"
        )

    hsp_drift: list[dict[str, object]] = []
    shared = sorted(set(ref_keys) & set(cand_keys))
    for key in shared:
        rh = ref_keys[key]
        ch = cand_keys[key]
        subject_differences: dict[str, object] = {}
        for field_name in (
            "rank",
            "subject_id",
            "accession",
            "hit_id",
            "hit_def",
            "hit_len",
            "organism",
        ):
            left = getattr(rh, field_name)
            right = getattr(ch, field_name)
            if left != right:
                subject_differences[field_name] = {"reference": left, "candidate": right}
        if len(rh.hsps) != len(ch.hsps):
            subject_differences["hsp_count"] = {
                "reference": len(rh.hsps),
                "candidate": len(ch.hsps),
            }
        if subject_differences:
            hsp_drift.append({"accession": key, "subject": subject_differences})
        for hsp_index, (ref_hsp, candidate_hsp) in enumerate(
            zip(rh.hsps, ch.hsps, strict=False),
            start=1,
        ):
            differences = _hsp_differences(
                ref_hsp,
                candidate_hsp,
                evalue_rel_tol=evalue_rel_tol,
                bit_score_rel_tol=bit_score_rel_tol,
            )
            if differences:
                hsp_drift.append(
                    {
                        "accession": key,
                        "hsp_index": hsp_index,
                        **differences,
                    }
                )

    if hsp_drift:
        exact_findings.append(f"{len(hsp_drift)} subject/HSP records drifted beyond tolerance")

    comparison_mode: Literal["strict_exact", "drift_tolerant_containment"] = (
        "drift_tolerant_containment" if tolerate_db_drift else "strict_exact"
    )
    findings = drift_findings if tolerate_db_drift else exact_findings
    exact_equivalent = not exact_findings
    drift_compatible = not drift_findings
    equivalent = drift_compatible if tolerate_db_drift else exact_equivalent
    snapshot_drift_detail = (
        assess_snapshot_drift(
            candidate.database,
            candidate.db_num,
            candidate.db_len,
        )
        if candidate.database
        else None
    )
    return ParityReport(
        equivalent=equivalent,
        snapshot_drift=snapshot_drift,
        db_len_representation_normalized=db_len_representation_normalized,
        exact_equivalent=exact_equivalent,
        drift_compatible=drift_compatible,
        comparison_mode=comparison_mode,
        findings=findings,
        exact_findings=exact_findings,
        reference_db_num=reference.db_num,
        candidate_db_num=candidate.db_num,
        reference_rank_count=len(reference.hits),
        candidate_rank_count=len(candidate.hits),
        rank_set_only_in_reference=only_ref,
        rank_set_only_in_candidate=only_cand,
        hsp_drift=hsp_drift,
        snapshot_drift_detail=snapshot_drift_detail,
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
            target = f"{hit.organism}\n{hit.hit_id}\n{hit.hit_def}"
            if marker.lower() in target.lower():
                violations.append(
                    f"rank {hit.rank}: excluded marker {marker!r} found in "
                    f"organism/{hit.organism!r}"
                )
                break
    return violations


def verify_xml2_taxid_exclusion(
    deflines: Iterable[WebBlastDefline],
    *,
    forbidden_taxids: Iterable[int],
) -> list[dict[str, object]]:
    """Return structured missing/forbidden-taxid findings for XML2 deflines.

    ``forbidden_taxids`` must contain the requested excluded taxid and every
    descendant resolved from an authoritative taxonomy snapshot. This function
    deliberately performs no title inference and no network lookup.
    """
    forbidden = {int(taxid) for taxid in forbidden_taxids if int(taxid) > 0}
    if not forbidden:
        raise ValueError("forbidden_taxids must contain at least one positive taxid")
    findings: list[dict[str, object]] = []
    for defline in deflines:
        common = {
            "hit_rank": defline.hit_rank,
            "descriptor_index": defline.descriptor_index,
            "accession": defline.accession,
            "identifier": defline.identifier,
            "scientific_name": defline.scientific_name,
        }
        if defline.taxid is None:
            findings.append({"code": "missing_taxid", **common})
        elif defline.taxid in forbidden:
            findings.append(
                {
                    "code": "excluded_taxid_present",
                    **common,
                    "taxid": defline.taxid,
                }
            )
    return findings
