"""Tests for the BLAST database snapshot-drift verdict service.

Responsibility: cover the match / drift / uncalibrated / unknown branches of
`assess_snapshot_drift` and the `is_calibrated_database` helper.
Edit boundaries: pure unit tests; no Azure, network, or Storage.
Key entry points: `test_*` functions below.
Risky contracts: the calibrated core_nt numbers must stay in sync with
`api/services/web_blast_searchsp.py`.
Validation: `uv run pytest -q api/tests/test_snapshot_drift.py`.
"""

from __future__ import annotations

from api.services.blast.snapshot_drift import assess_snapshot_drift
from api.services.web_blast_searchsp import is_calibrated_database

CORE_NT_DB_NUM = 125_619_662
CORE_NT_DB_LEN = 1_041_443_571_674


def test_match_when_observed_equals_calibration() -> None:
    verdict = assess_snapshot_drift("core_nt", CORE_NT_DB_NUM, CORE_NT_DB_LEN)
    assert verdict["status"] == "match"
    assert verdict["database"] == "core_nt"
    assert verdict["db_num_delta_pct"] == 0.0
    assert verdict["db_len_delta_pct"] == 0.0
    assert verdict["calibrated_db_num"] == CORE_NT_DB_NUM
    assert verdict["calibrated_db_len"] == CORE_NT_DB_LEN


def test_match_within_tolerance() -> None:
    # 0.1% growth in both dimensions stays under the 0.5% default tolerance.
    verdict = assess_snapshot_drift(
        "core_nt",
        int(CORE_NT_DB_NUM * 1.001),
        int(CORE_NT_DB_LEN * 1.001),
    )
    assert verdict["status"] == "match"


def test_drift_when_observed_exceeds_tolerance() -> None:
    # 5% growth is well beyond tolerance.
    verdict = assess_snapshot_drift(
        "core_nt",
        int(CORE_NT_DB_NUM * 1.05),
        int(CORE_NT_DB_LEN * 1.05),
    )
    assert verdict["status"] == "drift"
    assert verdict["db_num_delta_pct"] is not None
    assert verdict["db_num_delta_pct"] > 0
    assert "drift" in verdict["message"].lower()


def test_drift_path_is_resolved_to_name() -> None:
    verdict = assess_snapshot_drift(
        "https://acct.blob.core.windows.net/blastdb/core_nt",
        int(CORE_NT_DB_NUM * 1.2),
        CORE_NT_DB_LEN,
    )
    assert verdict["database"] == "core_nt"
    assert verdict["status"] == "drift"


def test_uncalibrated_database() -> None:
    verdict = assess_snapshot_drift("nt_prok", 10, 20)
    assert verdict["status"] == "uncalibrated"
    assert verdict["calibrated_db_num"] is None
    assert verdict["db_num_delta_pct"] is None
    assert "cannot be asserted" in verdict["message"]


def test_unknown_when_observed_missing() -> None:
    verdict = assess_snapshot_drift("core_nt", None, None)
    assert verdict["status"] == "unknown"
    assert verdict["calibrated_db_num"] == CORE_NT_DB_NUM
    assert verdict["db_num_delta_pct"] is None


def test_unknown_when_observed_zero() -> None:
    verdict = assess_snapshot_drift("core_nt", 0, 0)
    assert verdict["status"] == "unknown"


def test_is_calibrated_database() -> None:
    assert is_calibrated_database("core_nt") is True
    assert is_calibrated_database("https://x/blastdb/core_nt") is True
    assert is_calibrated_database("nt_prok") is False
