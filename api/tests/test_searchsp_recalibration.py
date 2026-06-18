"""Tests for inline Web BLAST search-space drift recalibration in the submit gate.

Module summary: Locks the auto-recalibration behaviour of `resolve_sharding_plan`
— when the caller forwards live DB stats (db_total_letters / db_total_sequences),
the verified search space is recomputed from them so a drifted core_nt snapshot
stays precise, while a caller that replays the stale pinned value against drifted
stats degrades gracefully.
Responsibility: Guard the gate's recompute-accept and snapshot-drift-degrade
branches plus the no-stats pinned fallback.
Edit boundaries: Test-only; no production state.
Key entry points: `resolve_sharding_plan`, `compute_web_blast_searchsp`.
Risky contracts: The recompute-accept path is what makes browser New Search work
after the DB drifts without manual recalibration — if it regresses, precise runs
silently degrade to approximate.
Validation: `uv run pytest -q api/tests/test_searchsp_recalibration.py`.
"""

from __future__ import annotations

from api.services.blast.submit_payload import resolve_sharding_plan
from api.services.web_blast_searchsp import (
    WEB_BLAST_SEARCHSP_DEFAULTS,
    compute_web_blast_searchsp,
)

_CORE_NT = WEB_BLAST_SEARCHSP_DEFAULTS["core_nt"]
# A drifted (re-downloaded, slightly larger) core_nt snapshot.
_DRIFT_DB_LEN = _CORE_NT.calibrated_db_len + 5_000_000_000
_DRIFT_DB_NUM = _CORE_NT.calibrated_db_num + 600_000
_DRIFT_SEARCHSP = compute_web_blast_searchsp(_DRIFT_DB_LEN, _DRIFT_DB_NUM)


def _precise_options(**overrides: object) -> dict[str, object]:
    options: dict[str, object] = {
        "sharding_mode": "precise",
        "outfmt": 5,
        "query_count": 1,
    }
    options.update(overrides)
    return options


def test_drifted_snapshot_recomputed_searchsp_stays_precise() -> None:
    """A browser submit that forwards the live (drifted) stats and the matching
    recomputed search space stays precise — no manual recalibration needed."""
    assert _DRIFT_SEARCHSP is not None
    assert _DRIFT_SEARCHSP != _CORE_NT.value  # the snapshot really drifted

    plan = resolve_sharding_plan(
        program="blastn",
        database="blast-db/core_nt/core_nt",
        options=_precise_options(
            db_effective_search_space=_DRIFT_SEARCHSP,
            db_total_letters=_DRIFT_DB_LEN,
            db_total_sequences=_DRIFT_DB_NUM,
        ),
        caller_supplied_searchsp=_DRIFT_SEARCHSP,
    )

    assert plan.validation_errors == []
    assert plan.downgraded is False
    assert plan.options["db_effective_search_space"] == _DRIFT_SEARCHSP
    contract = plan.compatibility_contract
    assert contract["mode"] == "precise"
    assert contract["level"] == "web_blast_compatible_sharded"


def test_stale_pinned_value_against_drifted_stats_degrades() -> None:
    """A non-browser caller replaying the pinned value while the DB drifted is a
    genuine snapshot drift — it degrades to approximate on every surface."""
    plan = resolve_sharding_plan(
        program="blastn",
        database="blast-db/core_nt/core_nt",
        options=_precise_options(
            db_effective_search_space=_CORE_NT.value,
            db_total_letters=_DRIFT_DB_LEN,
            db_total_sequences=_DRIFT_DB_NUM,
        ),
        caller_supplied_searchsp=_CORE_NT.value,
    )

    assert plan.downgraded is True
    assert plan.validation_errors == []
    assert "db_effective_search_space" not in plan.options
    assert plan.options["sharding_mode"] == "approximate"


def test_no_live_stats_falls_back_to_pinned_value() -> None:
    """Without live stats the pinned calibration value is accepted as before."""
    plan = resolve_sharding_plan(
        program="blastn",
        database="blast-db/core_nt/core_nt",
        options=_precise_options(
            db_effective_search_space=_CORE_NT.value,
            db_total_letters=_CORE_NT.calibrated_db_len,
        ),
        caller_supplied_searchsp=_CORE_NT.value,
    )

    assert plan.validation_errors == []
    assert plan.downgraded is False
    assert plan.options["db_effective_search_space"] == _CORE_NT.value
    assert plan.compatibility_contract["mode"] == "precise"


def test_genuine_bad_override_still_blocks_browser() -> None:
    """A caller value that matches neither the recomputed nor the pinned search
    space is a bad override and keeps blocking (no Service Bus downgrade)."""
    plan = resolve_sharding_plan(
        program="blastn",
        database="blast-db/core_nt/core_nt",
        options=_precise_options(
            db_effective_search_space=42,
            db_total_letters=_DRIFT_DB_LEN,
            db_total_sequences=_DRIFT_DB_NUM,
        ),
        caller_supplied_searchsp=42,
    )

    assert plan.downgraded is False
    assert plan.validation_errors
