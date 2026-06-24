"""Tests for CSV/TSV formula-injection (CSV injection) defence.

Responsibility: lock the pure neutralisation contract in
`api/services/blast/csv_safety.py` — leading formula triggers get an apostrophe,
typed numbers and benign text pass through untouched.
Edit boundaries: pure-function tests only; the export wiring is covered in
`test_blast_results_routes.py` and `test_result_transcode.py`.
Key entry points: the test functions below.
Risky contracts: numeric cells must NOT be mutated; only str cells starting with
a trigger char are escaped.
Validation: `uv run pytest -q api/tests/test_csv_safety.py`.
"""

from __future__ import annotations

import pytest
from api.services.blast.csv_safety import csv_safe_cell, csv_safe_cells, csv_safe_row


@pytest.mark.parametrize(
    "value,expected",
    [
        ("=cmd()", "'=cmd()"),
        ("+1+1", "'+1+1"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tinjected", "'\tinjected"),
        ("\rinjected", "'\rinjected"),
        ("NR_123456.1", "NR_123456.1"),
        ("Monkeypox virus", "Monkeypox virus"),
        ("", ""),
        # ``-`` is intentionally NOT a trigger: gap-leading sequences and
        # negative-number strings are legitimate BLAST data and must survive.
        ("-ACGTACGT", "-ACGTACGT"),
        ("-1e-50", "-1e-50"),
    ],
)
def test_csv_safe_cell(value: str, expected: str) -> None:
    assert csv_safe_cell(value) == expected


def test_csv_safe_cell_leaves_numbers_untouched() -> None:
    # Typed numeric cells are never strings → never escaped (an evalue of
    # -1e-50 stays a float, not "'-1e-50").
    assert csv_safe_cell(-1.5e-50) == -1.5e-50
    assert csv_safe_cell(370) == 370
    assert csv_safe_cell(None) is None


def test_csv_safe_row() -> None:
    row = {"sseqid": "=HYPERLINK(1)", "evalue": 1e-50, "pident": 99.5}
    safe = csv_safe_row(row)
    assert safe["sseqid"] == "'=HYPERLINK(1)"
    assert safe["evalue"] == 1e-50
    assert safe["pident"] == 99.5


def test_csv_safe_cells_positional() -> None:
    assert csv_safe_cells(["=evil", "ok", 12]) == ["'=evil", "ok", 12]
