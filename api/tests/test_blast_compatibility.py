"""Tests for BLAST Compatibility behavior.

Responsibility: Tests for BLAST Compatibility behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_core_nt_precise_contract_uses_verified_default`,
`test_unknown_db_precise_contract_requires_calibration_even_with_searchsp`,
`test_explicit_searchsp_mismatch_invalidates_verified_evidence`,
`test_verified_db_nondefault_search_space_runs_without_precise_claim`,
`test_explicit_matching_searchsp_is_precise_eligible`,
`test_approximate_contract_remains_eligible_with_warning`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_compatibility.py`.
"""

from __future__ import annotations

from api.services.blast.compatibility import build_compatibility_contract
from api.services.sharding_precision import build_precision_report


def test_core_nt_precise_contract_uses_verified_default() -> None:
    options = {
        "sharding_mode": "precise",
        "outfmt": 5,
        "query_count": 1,
        "db_effective_search_space": 32_156_241_807_668,
        "db_total_letters": 1_041_443_571_674,
    }
    precision = build_precision_report(options, query_count=1, db_stats_available=True)

    contract = build_compatibility_contract(
        database="blast-db/core_nt/core_nt",
        options=options,
        precision_report=precision,
    )

    assert contract.mode == "precise"
    assert contract.eligible is True
    assert contract.level == "web_blast_compatible_sharded"
    assert contract.search_space_source == "verified_default"
    assert contract.evidence is not None
    assert contract.evidence["db_name"] == "core_nt"
    assert contract.evidence["blast_version"] == "BLASTN 2.17.0+"
    assert "core_nt 2026-05-09" in str(contract.evidence["database_snapshot"])


def test_unknown_db_precise_contract_requires_calibration_even_with_searchsp() -> None:
    options = {
        "sharding_mode": "precise",
        "outfmt": 5,
        "query_count": 1,
        "db_effective_search_space": 12345,
        "db_total_letters": 99999,
    }
    precision = build_precision_report(options, query_count=1, db_stats_available=True)

    contract = build_compatibility_contract(
        database="unknown_nt",
        options=options,
        precision_report=precision,
    )

    assert contract.mode == "calibration_required"
    assert contract.eligible is False
    assert contract.evidence is None
    assert "verified database search-space evidence" in contract.blocking_errors[0]


def test_explicit_searchsp_mismatch_invalidates_verified_evidence() -> None:
    options = {
        "sharding_mode": "precise",
        "outfmt": 5,
        "query_count": 1,
        "additional_options": "-searchsp 42",
        "db_effective_search_space": 42,
        "db_total_letters": 1_041_443_571_674,
    }
    precision = build_precision_report(options, query_count=1, db_stats_available=True)

    contract = build_compatibility_contract(
        database="core_nt",
        options=options,
        precision_report=precision,
    )

    assert contract.mode == "calibration_required"
    assert contract.eligible is False
    assert contract.search_space_source == "explicit_override"
    assert "does not match verified" in contract.blocking_errors[0]


def test_verified_db_nondefault_search_space_runs_without_precise_claim() -> None:
    options = {
        "sharding_mode": "precise",
        "outfmt": 5,
        "query_count": 1,
        "db_effective_search_space": 123456,
        "db_total_letters": 123456,
    }
    precision = build_precision_report(options, query_count=1, db_stats_available=True)

    contract = build_compatibility_contract(
        database="core_nt",
        options=options,
        precision_report=precision,
    )

    assert precision.eligible is True
    assert contract.mode == "calibration_required"
    assert contract.eligible is True
    assert contract.level == "verified_database_nondefault_search_space"
    assert contract.blocking_errors == []
    assert any("mechanically precise" in warning for warning in contract.warnings)


def test_explicit_matching_searchsp_is_precise_eligible() -> None:
    options = {
        "sharding_mode": "precise",
        "outfmt": 5,
        "query_count": 1,
        "additional_options": "-searchsp 32156241807668",
        "db_total_letters": 1_041_443_571_674,
    }
    precision = build_precision_report(options, query_count=1, db_stats_available=True)

    contract = build_compatibility_contract(
        database="core_nt",
        options=options,
        precision_report=precision,
    )

    assert precision.eligible is True
    assert contract.mode == "precise"
    assert contract.eligible is True
    assert contract.search_space_source == "explicit_override"


def test_approximate_contract_remains_eligible_with_warning() -> None:
    options = {"sharding_mode": "approximate", "outfmt": 6, "db_auto_partition": True}
    precision = build_precision_report(options, query_count=None, db_stats_available=False)

    contract = build_compatibility_contract(
        database="unknown_nt",
        options=options,
        precision_report=precision,
    )

    assert contract.mode == "approximate"
    assert contract.eligible is True
    assert contract.level == "approximate_sharded"
    assert any("may differ" in warning for warning in contract.warnings)


def test_unsharded_unknown_db_runs_but_is_not_precise() -> None:
    options = {"sharding_mode": "off", "outfmt": 5}
    precision = build_precision_report(options, query_count=1, db_stats_available=False)

    contract = build_compatibility_contract(
        database="labdb",
        options=options,
        precision_report=precision,
    )

    assert contract.mode == "calibration_required"
    assert contract.eligible is True
    assert contract.level == "unverified_full_database"
    assert contract.blocking_errors == []
