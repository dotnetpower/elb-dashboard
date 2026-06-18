"""Tests for the Web BLAST search-space recompute helper.

Module summary: Locks in `compute_web_blast_searchsp` against the pinned core_nt
calibration so the snapshot-drift recalibration math stays correct: the helper
must reproduce the verified `value` from the calibrated db_len/db_num, and must
degrade to None on degenerate inputs.
Responsibility: Guard the deterministic effective-search-space formula that a
future drift recalibration will use to refresh a calibrated DB's search space.
Edit boundaries: Test-only; no production state.
Key entry points: `compute_web_blast_searchsp`.
Risky contracts: The reproduction assertion is the acceptance test for the whole
auto-recalibration approach — if it ever fails, the formula or the pinned
constants drifted and recalibration must not be trusted.
Validation: `uv run pytest -q api/tests/test_web_blast_searchsp.py`.
"""

from __future__ import annotations

from api.services.web_blast_searchsp import (
    WEB_BLAST_SEARCHSP_DEFAULTS,
    compute_web_blast_searchsp,
)


def test_compute_web_blast_searchsp_reproduces_pinned_value() -> None:
    """The formula must reproduce the pinned core_nt value EXACTLY from its
    calibrated db_len / db_num — the acceptance test for drift recalibration."""
    default = WEB_BLAST_SEARCHSP_DEFAULTS["core_nt"]
    assert default.calibrated_db_len is not None
    assert default.calibrated_db_num is not None

    recomputed = compute_web_blast_searchsp(
        default.calibrated_db_len,
        default.calibrated_db_num,
    )
    assert recomputed == default.value == 32_156_241_807_668


def test_compute_web_blast_searchsp_tracks_db_growth() -> None:
    """A larger (drifted) snapshot yields a proportionally larger search space."""
    default = WEB_BLAST_SEARCHSP_DEFAULTS["core_nt"]
    grown = compute_web_blast_searchsp(
        int(default.calibrated_db_len * 1.02),
        int(default.calibrated_db_num * 1.02),
    )
    assert grown is not None
    assert grown > default.value


def test_compute_web_blast_searchsp_rejects_degenerate_inputs() -> None:
    assert compute_web_blast_searchsp(0, 100) is None
    assert compute_web_blast_searchsp(1_000, 0) is None
    # db_num so large that the effective db length goes non-positive.
    assert compute_web_blast_searchsp(100, 100) is None
    # query_len <= length adjustment → non-positive effective query length.
    assert compute_web_blast_searchsp(1_000_000, 10, query_len=33) is None
