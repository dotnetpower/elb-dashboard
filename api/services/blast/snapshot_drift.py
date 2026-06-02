"""Quantify drift between a BLAST run's observed database snapshot and the
verified NCBI Web BLAST calibration.

Responsibility: compare a run's observed `Statistics_db-num` / `Statistics_db-len`
against the calibrated snapshot recorded in `web_blast_searchsp` and return a
structured, quantified verdict (match / drift / uncalibrated / unknown).
Edit boundaries: keep this module side-effect-free and dependency-light. It
only reads the static calibration registry and returns a plain dict; do not
add Azure SDK, network, or Storage calls here.
Key entry points: `assess_snapshot_drift`, `DriftStatus`.
Risky contracts: parity claims against NCBI Web BLAST are only valid when the
local database snapshot matches the calibrated one. A `drift` verdict means
e-values and the hit-count tail may differ from NCBI even though the request
options are identical. An `uncalibrated` verdict means no equivalence baseline
exists for the database at all.
Validation: `uv run pytest -q api/tests/test_snapshot_drift.py`.
"""

from __future__ import annotations

from typing import Final, Literal

from api.services.web_blast_searchsp import (
    database_name_from_path,
    default_for_database,
)

DriftStatus = Literal["match", "drift", "uncalibrated", "unknown"]

# Default relative tolerance for treating an observed vs calibrated database
# size difference as a match. NCBI refreshes `core_nt` incrementally, so a tiny
# growth between snapshots is expected; anything above this is reported as
# drift so the operator knows the statistical model has moved.
DEFAULT_REL_TOL: Final[float] = 0.005  # 0.5%


def _delta_pct(observed: int | None, calibrated: int | None) -> float | None:
    if not observed or not calibrated:
        return None
    return round((observed - calibrated) * 100.0 / calibrated, 4)


def assess_snapshot_drift(
    database: str,
    observed_db_num: int | None,
    observed_db_len: int | None,
    *,
    rel_tol: float = DEFAULT_REL_TOL,
) -> dict[str, object]:
    """Compare a run's observed DB statistics against the calibrated snapshot.

    Args:
        database: dashboard DB path, URL, or bare name (e.g. ``core_nt``).
        observed_db_num: ``Statistics_db-num`` (sequence count) from the run's
            result XML, or ``None`` when unavailable.
        observed_db_len: ``Statistics_db-len`` (total bases) from the run's
            result XML, or ``None`` when unavailable.
        rel_tol: relative tolerance below which a size difference is a match.

    Returns:
        A structured verdict dict. ``status`` is one of:

        * ``uncalibrated`` — the database has no verified Web BLAST equivalence
          baseline; parity with NCBI cannot be asserted.
        * ``unknown`` — calibration exists but the run did not report usable
          ``db-num`` / ``db-len`` statistics, so drift cannot be measured.
        * ``match`` — observed statistics are within ``rel_tol`` of the
          calibrated snapshot; NCBI-equivalent statistics are expected.
        * ``drift`` — observed statistics differ beyond ``rel_tol``; e-values
          and the hit-count tail may differ from NCBI Web BLAST.
    """
    name = database_name_from_path(database)
    calibration = default_for_database(database)
    if calibration is None:
        return {
            "database": name,
            "status": "uncalibrated",
            "calibrated_db_num": None,
            "calibrated_db_len": None,
            "observed_db_num": observed_db_num,
            "observed_db_len": observed_db_len,
            "db_num_delta_pct": None,
            "db_len_delta_pct": None,
            "message": (
                f"No verified NCBI Web BLAST calibration exists for {name or 'this database'!r}; "
                "result parity with NCBI cannot be asserted."
            ),
        }

    calibrated_db_num = calibration.calibrated_db_num
    calibrated_db_len = calibration.calibrated_db_len
    db_num_delta = _delta_pct(observed_db_num, calibrated_db_num)
    db_len_delta = _delta_pct(observed_db_len, calibrated_db_len)

    if db_num_delta is None and db_len_delta is None:
        return {
            "database": name,
            "status": "unknown",
            "calibrated_db_num": calibrated_db_num,
            "calibrated_db_len": calibrated_db_len,
            "observed_db_num": observed_db_num,
            "observed_db_len": observed_db_len,
            "db_num_delta_pct": None,
            "db_len_delta_pct": None,
            "message": (
                "The run did not report database statistics, so snapshot drift "
                f"against the calibrated {name} snapshot could not be measured."
            ),
        }

    tol_pct = rel_tol * 100.0
    drifted = any(
        delta is not None and abs(delta) > tol_pct
        for delta in (db_num_delta, db_len_delta)
    )
    status: DriftStatus = "drift" if drifted else "match"
    if status == "match":
        message = (
            f"Observed {name} snapshot matches the NCBI calibration within "
            f"{tol_pct:g}%; NCBI-equivalent statistics are expected."
        )
    else:
        parts = []
        if db_num_delta is not None:
            parts.append(f"sequences {db_num_delta:+g}%")
        if db_len_delta is not None:
            parts.append(f"bases {db_len_delta:+g}%")
        message = (
            f"Observed {name} snapshot drifted from the NCBI calibration "
            f"({', '.join(parts)}); e-values and the hit-count tail may differ "
            "from NCBI Web BLAST."
        )

    return {
        "database": name,
        "status": status,
        "calibrated_db_num": calibrated_db_num,
        "calibrated_db_len": calibrated_db_len,
        "observed_db_num": observed_db_num,
        "observed_db_len": observed_db_len,
        "db_num_delta_pct": db_num_delta,
        "db_len_delta_pct": db_len_delta,
        "message": message,
    }
