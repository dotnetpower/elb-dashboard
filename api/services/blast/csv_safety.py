"""CSV / TSV formula-injection (CSV injection) defence for result exports.

Module summary: a spreadsheet (Excel / Google Sheets / LibreOffice) interprets a
cell whose first character is one of ``= + - @`` TAB CR as a *formula*, so an
export that echoes user- or database-influenced text (subject titles,
accessions, taxonomy strings) into CSV/TSV can smuggle a formula that executes on
open (OWASP "CSV injection" / "formula injection"). This neutralises it by
prefixing a single apostrophe so the cell renders as literal text — the standard
defence — applied uniformly to every delimited BLAST-result export.

Responsibility: neutralise leading formula triggers in delimited-export cells.
Edit boundaries: pure string helpers; no I/O, no FastAPI, no Azure SDK. Every
  CSV/TSV writer that emits parsed BLAST hit fields MUST route cells through here.
Key entry points: `csv_safe_cell`, `csv_safe_row`, `csv_safe_cells`.
Risky contracts: numeric cells (already int/float) pass through unchanged; only
  ``str`` cells starting with a trigger get a leading apostrophe. JSON / XML
  exports do NOT need this (no formula evaluation) and must not call it.
Validation: `uv run pytest -q api/tests/test_csv_safety.py`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# OWASP CSV-injection trigger characters: a leading one makes a spreadsheet treat
# OWASP CSV-injection trigger characters: a leading one makes a spreadsheet treat
# the cell as a formula. TAB / CR are included because a naive parser can split
# on them to reach a formula in the following field.
#
# ``-`` is deliberately EXCLUDED: a BLAST alignment ``qseq`` / ``sseq`` can
# legitimately begin with a gap (``-``) and an unparsed numeric cell can be a
# negative value (``-1e-50``), so escaping a leading ``-`` would corrupt real
# scientific data. The data sources here are trusted (the NCBI database + the
# caller's own query), making this defence defence-in-depth rather than a live
# threat block, so the rare ``-`` formula vector is not worth mangling sequences.
_FORMULA_TRIGGERS = frozenset("=+@\t\r")


def csv_safe_cell(value: Any) -> Any:
    """Prefix a leading apostrophe when ``value`` is a string that would be read
    as a spreadsheet formula. Non-strings (already-typed numbers) pass through.
    """
    if isinstance(value, str) and value and value[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def csv_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``row`` with every value neutralised for CSV injection."""
    return {key: csv_safe_cell(value) for key, value in row.items()}


def csv_safe_cells(cells: Iterable[Any]) -> list[Any]:
    """Neutralise every cell in a positional row (``csv.writer`` style)."""
    return [csv_safe_cell(cell) for cell in cells]
