"""Derive query identity (length + molecule type) from submitted FASTA.

Module summary: a Service Bus / external-API BLAST job carries its query as
inline FASTA at submit time, but the sibling ``/v1/jobs`` record never echoes
the query length or molecule type back, so the job detail showed "Query length —"
and "Molecule —". This helper derives a small ``query_meta`` dict from the FASTA
the dashboard already received, stamped durably on the job row by the drain.

Responsibility: pure parse of FASTA text into ``{length, molecule, records}``.
Edit boundaries: pure function; no IO, no Azure, no FastAPI.
Key entry points: ``query_meta_from_fasta``.
Risky contracts: ``molecule`` is a heuristic (nucleotide vs protein) — it must
  degrade to ``""`` (unknown) rather than guess wrong on an empty/odd sequence.
Validation: ``uv run pytest -q api/tests/test_external_query_meta.py``.
"""

from __future__ import annotations

from typing import Any

# Nucleotide alphabet (IUPAC), upper-cased. A sequence whose letters are almost
# entirely within this set is treated as a nucleotide query; otherwise protein.
_NUCLEOTIDE_LETTERS = frozenset("ACGTUNRYSWKMBDHV")
_NUCLEOTIDE_FRACTION = 0.9
_MAX_SCAN_CHARS = 2_000_000  # cap the molecule-heuristic scan on a huge FASTA


def query_meta_from_fasta(query_fasta: Any) -> dict[str, Any]:
    """Return ``{length, molecule, records}`` derived from inline FASTA text.

    ``length`` is the total residue count across all records (excluding headers
    and whitespace), ``records`` the number of ``>`` headers, and ``molecule`` is
    ``"nucleotide"`` / ``"protein"`` / ``""`` (unknown). Returns ``{}`` for
    empty / non-string input so the caller stamps nothing.
    """
    if not isinstance(query_fasta, str) or not query_fasta.strip():
        return {}
    total_len = 0
    records = 0
    nucleotide_hits = 0
    scanned = 0
    for raw_line in query_fasta.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(">"):
            records += 1
            continue
        seq = line.replace(" ", "")
        total_len += len(seq)
        if scanned < _MAX_SCAN_CHARS:
            for ch in seq.upper():
                if ch.isalpha():
                    scanned += 1
                    if ch in _NUCLEOTIDE_LETTERS:
                        nucleotide_hits += 1
                    if scanned >= _MAX_SCAN_CHARS:
                        break
    if total_len == 0:
        return {}
    molecule = ""
    if scanned > 0:
        molecule = (
            "nucleotide"
            if nucleotide_hits / scanned >= _NUCLEOTIDE_FRACTION
            else "protein"
        )
    meta: dict[str, Any] = {"length": total_len, "records": max(1, records)}
    if molecule:
        meta["molecule"] = molecule
    return meta
