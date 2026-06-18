"""Verified Web BLAST-compatible search-space defaults.

Responsibility: Verified Web BLAST-compatible search-space defaults
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `WebBlastSearchSpaceDefault`, `database_name_from_path`,
`default_for_database`, `is_calibrated_database`, `compute_web_blast_searchsp`,
`calibrated_searchsp_for_stats`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class WebBlastSearchSpaceDefault:
    db_name: str
    value: int
    scope: str
    evidence: str
    blast_version: str
    database_snapshot: str
    option_scope: str
    revalidate_when: str
    # Machine-comparable form of the calibrated `database_snapshot` string.
    # `calibrated_db_num` is the BLAST `Statistics_db-num` (sequence count) and
    # `calibrated_db_len` is `Statistics_db-len` (total bases) measured for the
    # snapshot the search space was calibrated against. They let a run's
    # observed statistics be compared numerically against the calibration
    # instead of parsing the human-readable `database_snapshot` text. Optional
    # for forward compatibility: a future entry may register a search-space
    # default before its exact db counts are captured.
    calibrated_db_num: int | None = None
    calibrated_db_len: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "db_name": self.db_name,
            "value": self.value,
            "scope": self.scope,
            "evidence": self.evidence,
            "blast_version": self.blast_version,
            "database_snapshot": self.database_snapshot,
            "option_scope": self.option_scope,
            "revalidate_when": self.revalidate_when,
            "calibrated_db_num": self.calibrated_db_num,
            "calibrated_db_len": self.calibrated_db_len,
        }


WEB_BLAST_SEARCHSP_DEFAULTS: dict[str, WebBlastSearchSpaceDefault] = {
    "core_nt": WebBlastSearchSpaceDefault(
        db_name="core_nt",
        value=32_156_241_807_668,
        scope=(
            "blastn, core_nt 2026-05-09 snapshot, 64 nt calibration query, "
            "word_size=28, dust=yes, evalue=10, max_target_seqs=500, outfmt=5"
        ),
        evidence="docs/temp/core-nt-searchsp/core_nt-searchsp-calibration-results.tgz",
        blast_version="BLASTN 2.17.0+",
        database_snapshot=(
            "core_nt 2026-05-09; BLASTDB v5; 125,619,662 sequences; "
            "1,041,443,571,674 bases"
        ),
        option_scope="word_size=28, dust=yes, evalue=10, max_target_seqs=500, outfmt=5",
        revalidate_when=(
            "Recalibrate when BLAST+ version, database snapshot, query class, "
            "or option profile changes."
        ),
        # core_nt 2026-05-09 snapshot: 125,619,662 sequences / 1,041,443,571,674
        # bases (matches the `database_snapshot` string above).
        calibrated_db_num=125_619_662,
        calibrated_db_len=1_041_443_571_674,
    ),
}


def database_name_from_path(database: str) -> str:
    """Return the bare database name from a dashboard DB path or URL."""
    raw = (database or "").strip()
    if not raw:
        return ""
    if raw.startswith("https://"):
        parsed = urlparse(raw)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "blast-db":
            return parts[-1] if parts[-1] else parts[1]
        return parts[-1] if parts else ""
    raw = raw.removeprefix("blast-db/")
    parts = [part for part in raw.split("/") if part]
    return parts[-1] if parts else ""


def default_for_database(database: str) -> WebBlastSearchSpaceDefault | None:
    """Return the verified search-space default for a DB path/name, if known."""
    return WEB_BLAST_SEARCHSP_DEFAULTS.get(database_name_from_path(database))


def is_calibrated_database(database: str) -> bool:
    """Return True when the DB has a verified Web BLAST search-space default.

    Used by callers that want to flag results from databases for which no
    NCBI Web BLAST equivalence has been calibrated, so the UI/operator does
    not assume parity for an unverified database.
    """
    return default_for_database(database) is not None


# The verified Web BLAST search space for a calibrated database is just BLAST's
# effective search space for the fixed calibration query:
#
#     eff_searchsp = (query_len - L) * (db_len - db_num * L)
#
# where ``L`` is the Karlin-Altschul length adjustment. For core_nt the
# calibration query is 64 nt and ``L`` was measured at 33 — this reproduces the
# pinned ``value`` (32,156,241,807,668) EXACTLY from the 2026-05-09 snapshot's
# ``calibrated_db_len`` / ``calibrated_db_num`` (verified in
# ``test_compute_web_blast_searchsp_reproduces_pinned_value``). ``L`` is
# essentially insensitive to modest snapshot drift (it depends on ``log(db_len)``;
# a 5% core_nt growth leaves it at 33), so a refreshed snapshot's search space is
# recomputed from the same length adjustment and the new DB stats — no NCBI
# round-trip and no full-database BLAST run required.
CALIBRATION_QUERY_LEN = 64
CALIBRATION_LENGTH_ADJUSTMENT = 33


def compute_web_blast_searchsp(
    db_len: int,
    db_num: int,
    *,
    query_len: int = CALIBRATION_QUERY_LEN,
    length_adjustment: int = CALIBRATION_LENGTH_ADJUSTMENT,
) -> int | None:
    """Recompute the calibrated Web BLAST search space for new DB statistics.

    Returns the effective search space ``(query_len - L) * (db_len - db_num * L)``
    as a positive integer, or ``None`` when the inputs are non-positive or the
    length adjustment would drive either effective length to zero (a degenerate
    DB). Pure / deterministic — the building block a snapshot-drift recalibration
    uses to refresh a calibrated database's search space from its live
    ``total_letters`` (db_len) and ``total_sequences`` (db_num).
    """
    if db_len <= 0 or db_num <= 0 or query_len <= 0:
        return None
    eff_query = query_len - length_adjustment
    eff_db = db_len - db_num * length_adjustment
    if eff_query <= 0 or eff_db <= 0:
        return None
    return eff_query * eff_db


def calibrated_searchsp_for_stats(
    verified_default: WebBlastSearchSpaceDefault,
    db_len: int | None,
    db_num: int | None,
) -> int:
    """Resolve the verified Web BLAST search space for the LIVE database stats.

    When both live stats are present and the calibration formula yields a
    positive value, the search space is recomputed from them so it auto-adapts
    to snapshot drift (a re-downloaded, slightly larger/smaller core_nt). When
    either stat is missing or the formula degenerates, the pinned calibration
    ``value`` is returned unchanged. This is the single source of truth shared
    by the submit gate and the compatibility contract so both agree on the
    canonical value for any given live snapshot.
    """
    if db_len is not None and db_num is not None:
        recomputed = compute_web_blast_searchsp(db_len, db_num)
        if recomputed is not None:
            return recomputed
    return verified_default.value

