"""Extract Mode-B FASTA query templates from the SPA's New Search example file.

Responsibility: Parse ``web/src/pages/blastSubmit/queryExamples.ts`` and return
the ``(id, length, fasta)`` tuples so the concurrency harness reuses the exact
same FASTA payloads a researcher loads from "Load Query Example" — single source
of truth, no duplicated sequence data in the test tree.
Edit boundaries: Pure stdlib regex parsing of the TS template-literal objects.
No network, no Azure, no kubectl. If the TS schema changes shape, update the
``_OBJECT_RE`` / field regexes here only.
Key entry points: ``load_query_templates``, ``QueryTemplate``.
Risky contracts: Relies on each template object exposing ``id: "..."`` and a
backtick ``fasta`` block. ``core_nt`` filtering keys off the
``matchingDbs`` array containing ``"core_nt"``. orf1ab (21 kb) is returned like
any other; callers decide whether to include the heavy query.
Validation: ``uv run python scripts/e2e/concurrency/extract_queries.py`` prints
the parsed id/length table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TS_PATH = (
    Path(__file__).resolve().parents[3]
    / "web"
    / "src"
    / "pages"
    / "blastSubmit"
    / "queryExamples.ts"
)

# Each template object spans from an ``id:`` line to the closing ``},`` of the
# object. We match the whole object lazily, then pull individual fields out.
_OBJECT_RE = re.compile(r"\{\s*id:\s*\"(?P<id>[^\"]+)\".*?\},", re.DOTALL)
_LABEL_RE = re.compile(r"label:\s*\"([^\"]+)\"")
_MATCHING_RE = re.compile(r"matchingDbs:\s*\[([^\]]*)\]", re.DOTALL)
_PROGRAM_RE = re.compile(r"blastProgram:\s*\"([^\"]+)\"")
_LENGTH_RE = re.compile(r"length:\s*(\d+)")
_FASTA_RE = re.compile(r"fasta:\s*`(?P<fasta>.*?)`", re.DOTALL)


@dataclass(frozen=True)
class QueryTemplate:
    """One Mode-B query example reused verbatim from the SPA."""

    id: str
    label: str
    program: str
    length: int
    matching_dbs: tuple[str, ...]
    fasta: str


def load_query_templates(
    *, ts_path: Path | None = None, database: str | None = "core_nt"
) -> list[QueryTemplate]:
    """Parse the TS example file into ``QueryTemplate`` rows.

    When ``database`` is given, only templates whose ``matchingDbs`` contains it
    are returned (mirrors ``queryExamplesForDatabase`` in the SPA).
    """

    path = ts_path or _TS_PATH
    text = path.read_text(encoding="utf-8")
    out: list[QueryTemplate] = []
    for obj in _OBJECT_RE.finditer(text):
        block = obj.group(0)
        fasta_m = _FASTA_RE.search(block)
        if fasta_m is None:
            continue
        matching_m = _MATCHING_RE.search(block)
        matching = tuple(
            v.strip().strip('"')
            for v in (matching_m.group(1).split(",") if matching_m else [])
            if v.strip()
        )
        label_m = _LABEL_RE.search(block)
        program_m = _PROGRAM_RE.search(block)
        length_m = _LENGTH_RE.search(block)
        tpl = QueryTemplate(
            id=obj.group("id"),
            label=label_m.group(1) if label_m else obj.group("id"),
            program=program_m.group(1) if program_m else "blastn",
            length=int(length_m.group(1)) if length_m else 0,
            matching_dbs=matching,
            fasta=fasta_m.group("fasta").strip() + "\n",
        )
        if database is None or database in tpl.matching_dbs:
            out.append(tpl)
    return out


if __name__ == "__main__":
    rows = load_query_templates()
    print(f"core_nt templates: {len(rows)}")
    for r in rows:
        print(f"  {r.id:24s} {r.program:7s} len={r.length:6d} dbs={','.join(r.matching_dbs)}")
