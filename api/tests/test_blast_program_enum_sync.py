"""Guard that our BLAST program handling stays in sync with ElasticBLAST (#56).

Responsibility: Enforce roadmap R1' acceptance item 3 — fail when ElasticBLAST
advertises a BLAST program our submit-time handling has not added. A vendored
snapshot of the upstream program set is the CI-enforceable pin; an opt-in
sibling-source comparison keeps that snapshot honest whenever the
``elastic-blast-azure`` checkout is available.
Edit boundaries: Tests only — no runtime behaviour, no Azure, no network. Refresh
``ELASTIC_BLAST_ADVERTISED_PROGRAMS`` (and the compat map it guards) when the
sibling-comparison test flags drift.
Key entry points: `test_compat_map_covers_advertised_programs`,
`test_snapshot_matches_sibling_when_available`.
Risky contracts: The snapshot must mirror
``elastic_blast.util.ElbSupportedPrograms._programs`` in the sibling repo
``dotnetpower/elastic-blast-azure``.
Validation: `uv run pytest -q api/tests/test_blast_program_enum_sync.py`.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from api.services.blast.db_recommendation import _PROGRAM_TO_DB_MOLECULE

# Vendored snapshot of the BLAST programs ElasticBLAST advertises, mirroring
# ``ElbSupportedPrograms._programs`` in the sibling repo
# (``src/elastic_blast/util.py`` of ``dotnetpower/elastic-blast-azure``).
#
# This snapshot is the CI-enforceable pin: hosted CI has no sibling checkout, so
# ``test_compat_map_covers_advertised_programs`` runs against this list and fails
# if the program×database compatibility map (``_PROGRAM_TO_DB_MOLECULE``) ever
# stops covering an advertised program. ``test_snapshot_matches_sibling_when_
# available`` keeps the snapshot itself current: run it with the sibling repo
# checked out (default ``~/dev/elastic-blast-azure`` or ``ELB_ELASTIC_BLAST_SRC``)
# and it fails the moment upstream adds/removes a program, prompting a refresh
# here — which then forces the compat map to be updated too.
ELASTIC_BLAST_ADVERTISED_PROGRAMS: frozenset[str] = frozenset(
    {
        "blastn",
        "blastp",
        "blastx",
        "psiblast",
        "rpsblast",
        "rpstblastn",
        "tblastn",
        "tblastx",
    }
)


def _sibling_util_path() -> Path | None:
    """Resolve the sibling ``elastic_blast/util.py`` if a checkout is available.

    Honors ``ELB_ELASTIC_BLAST_SRC`` (repo root or the ``util.py`` file itself);
    otherwise falls back to the conventional local clone path. Returns ``None``
    when nothing usable is found so the comparison test can skip cleanly.
    """
    override = os.environ.get("ELB_ELASTIC_BLAST_SRC")
    candidates: list[Path] = []
    if override:
        override_path = Path(override).expanduser()
        candidates.append(override_path)
        candidates.append(override_path / "src" / "elastic_blast" / "util.py")
    candidates.append(
        Path.home() / "dev" / "elastic-blast-azure" / "src" / "elastic_blast" / "util.py"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _parse_sibling_programs(util_source: str) -> set[str]:
    """Extract ``ElbSupportedPrograms._programs`` from the sibling source via AST.

    Parsing the literal (rather than importing the sibling package, which is not
    a dependency of this repo) keeps the guard hermetic and import-safe.
    """
    tree = ast.parse(util_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ElbSupportedPrograms":
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                if "_programs" in targets and isinstance(stmt.value, ast.List):
                    programs: set[str] = set()
                    for element in stmt.value.elts:
                        if isinstance(element, ast.Constant) and isinstance(element.value, str):
                            programs.add(element.value.lower())
                    return programs
    raise AssertionError(
        "Could not locate ElbSupportedPrograms._programs in the sibling util.py; "
        "the upstream layout may have changed."
    )


def test_compat_map_covers_advertised_programs() -> None:
    """Every advertised ElasticBLAST program must be handled by the submit-time
    program×database compatibility guard.

    This is the CI-enforceable half: it runs without the sibling checkout, so a
    refreshed snapshot that adds a program forces ``_PROGRAM_TO_DB_MOLECULE`` to
    cover it before the build can go green.
    """
    handled = set(_PROGRAM_TO_DB_MOLECULE)
    missing = ELASTIC_BLAST_ADVERTISED_PROGRAMS - handled
    assert not missing, (
        "ElasticBLAST advertises BLAST programs that the program×database "
        f"compatibility map does not handle: {sorted(missing)}. Add them to "
        "_PROGRAM_TO_DB_MOLECULE in api/services/blast/db_recommendation.py."
    )


def test_snapshot_matches_sibling_when_available() -> None:
    """Keep the vendored snapshot honest against the live sibling source.

    Skips when the ``elastic-blast-azure`` checkout is not present (e.g. hosted
    CI). When it is present, any divergence fails so the snapshot above is
    refreshed the moment upstream changes its supported-program set.
    """
    util_path = _sibling_util_path()
    if util_path is None:
        pytest.skip(
            "elastic-blast-azure source not found; set ELB_ELASTIC_BLAST_SRC to "
            "the sibling repo to enable the upstream program-enum sync guard."
        )
    advertised = _parse_sibling_programs(util_path.read_text(encoding="utf-8"))
    assert advertised == set(ELASTIC_BLAST_ADVERTISED_PROGRAMS), (
        "ElasticBLAST's advertised BLAST programs drifted from the vendored "
        "snapshot. Update ELASTIC_BLAST_ADVERTISED_PROGRAMS in this file (and "
        "_PROGRAM_TO_DB_MOLECULE if a new program was added). "
        f"sibling={sorted(advertised)} snapshot={sorted(ELASTIC_BLAST_ADVERTISED_PROGRAMS)}"
    )
