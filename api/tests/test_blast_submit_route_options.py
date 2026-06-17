"""Tests for BLAST Submit Route Options behavior.

Responsibility: Tests for BLAST Submit Route Options behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_submit_options_forward_acr_fields`,
`test_submit_options_forward_taxid_filter_fields`, `test_submit_options_accept_searchsp_alias`,
`test_submit_options_forward_tie_order_oracle_controls`,
`test_submit_options_forward_db_order_oracle_opt_out`,
`test_submit_options_force_local_ssd_even_when_caller_disables_it`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_submit_route_options.py`.
"""

from __future__ import annotations

from api.routes._blast_shared import (
    _apply_web_blast_searchsp_default,
    _normalise_blast_submit_body,
    _submit_options_from_body,
)
from api.services.blast.submit_payload import canonical_execution_config, submit_contracts


def test_submit_options_forward_acr_fields() -> None:
    options = _submit_options_from_body(
        {
            "acr_name": "elbacr01",
            "acr_resource_group": "rg-elbacr-01",
            "machine_type": "Standard_E16s_v5",
        }
    )

    assert options["acr_name"] == "elbacr01"
    assert options["acr_resource_group"] == "rg-elbacr-01"
    assert options["machine_type"] == "Standard_E16s_v5"


def test_submit_options_forward_taxid_filter_fields() -> None:
    options = _submit_options_from_body(
        {
            "taxid": 3431483,
            "is_inclusive": False,
        }
    )

    assert options["taxid"] == 3431483
    assert options["is_inclusive"] is False


def test_submit_options_accept_searchsp_alias() -> None:
    options = _submit_options_from_body({"options": {"searchsp": "32156241807668"}})

    assert options["db_effective_search_space"] == "32156241807668"
    assert "searchsp" not in options


def test_submit_options_forward_tie_order_oracle_controls() -> None:
    options = _submit_options_from_body(
        {
            "tie_order_oracle_accessions": ["PX485240.1", "OX044342.2"],
            "tie_order_oracle_strict": True,
        }
    )

    assert options["tie_order_oracle_accessions"] == ["PX485240.1", "OX044342.2"]
    assert options["tie_order_oracle_strict"] is True


def test_submit_options_forward_db_order_oracle_opt_out() -> None:
    options = _submit_options_from_body({"use_db_order_oracle": False})

    assert options["use_db_order_oracle"] is False


def test_submit_options_force_local_ssd_even_when_caller_disables_it() -> None:
    options = _submit_options_from_body(
        {
            "options": {"use_local_ssd": False},
            "use_local_ssd": False,
        }
    )

    assert options["use_local_ssd"] is True


def test_normalise_submit_body_records_forced_local_ssd() -> None:
    body = {
        "resource_group": "rg-elb",
        "cluster_name": "elb-cluster",
        "storage_account": "elbstg01",
        "program": "blastn",
        "database": "blast-db/18S_fungal_sequences/18S_fungal_sequences",
        "query_file": "queries/q.fa",
        "use_local_ssd": False,
    }

    normalised = _normalise_blast_submit_body(body, job_id="job-1")

    assert normalised["use_local_ssd"] is True
    assert normalised["options"]["use_local_ssd"] is True


def test_normalise_submit_body_overrides_trusted_submit_metadata() -> None:
    body = {
        "resource_group": "rg-elb",
        "cluster_name": "elb-cluster",
        "storage_account": "elbstg01",
        "program": "blastn",
        "database": "core_nt",
        "query_file": "queries/q.fa",
        "submission_source": "external_api",
        "external_correlation_id": "caller-supplied",
        "priority": 80,
        "resource_profile": "widepool",
        "idempotency_key": "req-1",
    }

    normalised = _normalise_blast_submit_body(body, job_id="job-1")

    assert normalised["submission_source"] == "dashboard"
    assert normalised["external_correlation_id"] == "job-1"
    assert normalised["priority"] == 80
    assert normalised["resource_profile"] == "widepool"
    assert normalised["idempotency_key"] == "req-1"
    assert normalised["canonical_request"]["metadata"]["submission_source"] == "dashboard"


def test_ui_and_openapi_submit_shapes_share_execution_config() -> None:
    query = ">q1\nATGCATGCATGC\n"
    ui_payload = {
        "program": "blastn",
        "db": "core_nt",
        "query_data": query,
        "outfmt": 5,
        "word_size": 28,
        "low_complexity_filter": True,
        "evalue": 10.0,
        "max_target_seqs": 500,
    }
    openapi_payload = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": query,
        "options": {
            "outfmt": 5,
            "word_size": 28,
            "dust": True,
            "evalue": 10.0,
            "max_target_seqs": 500,
        },
    }

    assert canonical_execution_config(ui_payload) == canonical_execution_config(openapi_payload)


def test_ui_openapi_and_servicebus_precise_contracts_converge() -> None:
    from api.services.service_bus import ParsedMessage
    from api.services.service_bus_pref import ServiceBusConfig
    from api.services.web_blast_searchsp import WEB_BLAST_SEARCHSP_DEFAULTS
    from api.tasks.servicebus import tasks as sb_tasks

    query = ">q1\nATGCATGCATGC\n"
    ui_payload = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": query,
        "options": {"outfmt": 5, "sharding_mode": "precise"},
    }
    openapi_payload = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": query,
        "options": {"outfmt": 5, "sharding_mode": "precise"},
    }
    servicebus_payload = sb_tasks._build_request_payload(
        ParsedMessage(
            body={
                "program": "blastn",
                "db": "core_nt",
                "query_fasta": query,
                "options": {"outfmt": 5, "sharding_mode": "precise"},
                "external_correlation_id": "corr-converge",
            },
            raw_body="",
            message_id="m1",
            correlation_id="corr-converge",
            subject="blast.request",
            content_type="application/json",
            enqueued_time_utc=None,
            sequence_number=1,
            application_properties={},
        ),
        ServiceBusConfig(),
    )

    ui_contracts = submit_contracts(ui_payload)
    openapi_contracts = submit_contracts(openapi_payload)
    assert servicebus_payload is not None
    servicebus_contracts = submit_contracts(servicebus_payload)

    expected_searchsp = WEB_BLAST_SEARCHSP_DEFAULTS["core_nt"].value
    assert (
        canonical_execution_config(ui_payload)["options"]["db_effective_search_space"]
        == expected_searchsp
    )
    assert openapi_contracts["precision"]["required_options"] == servicebus_contracts["precision"][
        "required_options"
    ]
    assert ui_contracts["precision"]["precision_level"] == openapi_contracts["precision"][
        "precision_level"
    ] == servicebus_contracts["precision"]["precision_level"]
    assert ui_contracts["precision"]["merge_strategy"] == openapi_contracts["precision"][
        "merge_strategy"
    ] == servicebus_contracts["precision"]["merge_strategy"]
    assert ui_contracts["compatibility_contract"]["searchsp"] == expected_searchsp
    assert openapi_contracts["compatibility_contract"]["searchsp"] == expected_searchsp
    assert servicebus_contracts["compatibility_contract"]["searchsp"] == expected_searchsp


def test_browser_submit_degrades_on_calibration_snapshot_mismatch() -> None:
    """A browser New Search submit whose live core_nt stats no longer match the
    pinned Web BLAST calibration must degrade gracefully instead of hard-blocking.

    Regression for the New Search 'caller-supplied db_effective_search_space does
    not match the calibrated database snapshot' submission failure: the live
    core_nt DB drifted from the 2026-05-09 calibration snapshot, so the calibrated
    search space no longer applies. Every submit surface (browser included, not
    just the Service Bus bridge) now drops the calibrated value, falls back from
    precise to approximate sharding, and surfaces a warning rather than a 4xx.
    """
    body = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": ">q1\nATGCATGCATGC\n",
        # No submission_source → browser New Search path (not "servicebus").
        "options": {
            "outfmt": 5,
            "sharding_mode": "precise",
            # The pinned, verified calibrated search space …
            "db_effective_search_space": 32_156_241_807_668,
            # … but the live DB drifted from the 2026-05-09 snapshot
            # (calibrated_db_len = 1_041_443_571_674).
            "db_total_letters": 1_041_443_571_675,
        },
    }

    contracts = submit_contracts(body)

    # Not blocked — the calibration mismatch degrades instead of failing.
    assert contracts["compatibility_contract"].get("blocking_errors", []) == []
    assert contracts["precision"].get("blocking_errors", []) == []
    # Warned about the degrade.
    warnings = contracts["compatibility_contract"].get("warnings", [])
    assert any(
        "calibration" in str(w).lower() or "search space" in str(w).lower() for w in warnings
    ), warnings
    # Canonical options dropped the calibrated searchsp and fell back to approximate.
    opts = canonical_execution_config(body)["options"]
    assert "db_effective_search_space" not in opts
    assert opts["sharding_mode"] == "approximate"


def test_web_blast_searchsp_default_applies_for_core_nt() -> None:
    options: dict[str, object] = {}

    _apply_web_blast_searchsp_default("blast-db/core_nt/core_nt", options)

    assert options["db_effective_search_space"] == 32156241807668
    assert options["low_complexity_filter"] is True


def test_web_blast_searchsp_default_respects_explicit_override() -> None:
    options: dict[str, object] = {"additional_options": "-searchsp 42"}

    _apply_web_blast_searchsp_default("core_nt", options)

    assert "db_effective_search_space" not in options


def test_web_blast_defaults_preserve_low_complexity_opt_out() -> None:
    options: dict[str, object] = {"low_complexity_filter": False}

    _apply_web_blast_searchsp_default("core_nt", options)

    assert options["db_effective_search_space"] == 32156241807668
    assert options["low_complexity_filter"] is False


def test_submit_options_forward_ncbi_web_blast_algorithm_flags() -> None:
    """NCBI Web BLAST parity: matrix / threshold / composition / culling /
    best-hit / post-search filters must round-trip through the canonical
    options dict instead of being dropped or forced into the free-text
    ``additional_options`` escape hatch.

    Researchers expect to set these via the structured submit form (next
    wave) and via OpenAPI ``options`` today — both call paths funnel through
    ``_submit_options_from_body``, so a single assertion covers both.
    """
    options = _submit_options_from_body(
        {
            "matrix": "BLOSUM45",
            "threshold": 11,
            "comp_based_stats": 2,
            "culling_limit": 5,
            "best_hit_overhang": 0.25,
            "best_hit_score_edge": 0.1,
            "qcov_hsp_perc": 80,
            "perc_identity": 95.0,
            "window_size": 40,
            "xdrop_gap": 30,
            "xdrop_gap_final": 100,
            "xdrop_ungap": 20.0,
            "num_alignments": 250,
            "num_descriptions": 250,
            "parse_deflines": True,
            "soft_masking": True,
            "lcase_masking": True,
            "ungapped": False,
            "gilist": "gi-list-blob",
            "negative_gilist": "neg-gi-blob",
            "seqidlist": "seqid-blob",
        }
    )

    assert options["matrix"] == "BLOSUM45"
    assert options["threshold"] == 11
    assert options["comp_based_stats"] == 2
    assert options["culling_limit"] == 5
    assert options["best_hit_overhang"] == 0.25
    assert options["best_hit_score_edge"] == 0.1
    assert options["qcov_hsp_perc"] == 80
    assert options["perc_identity"] == 95.0
    assert options["window_size"] == 40
    assert options["xdrop_gap"] == 30
    assert options["xdrop_gap_final"] == 100
    assert options["xdrop_ungap"] == 20.0
    assert options["num_alignments"] == 250
    assert options["num_descriptions"] == 250
    assert options["parse_deflines"] is True
    assert options["soft_masking"] is True
    assert options["lcase_masking"] is True
    assert options["ungapped"] is False
    assert options["gilist"] == "gi-list-blob"
    assert options["negative_gilist"] == "neg-gi-blob"
    assert options["seqidlist"] == "seqid-blob"


def test_submit_options_via_options_dict_for_web_blast_parity_flags() -> None:
    """Same parity flags, this time wrapped in the OpenAPI ``options``
    sub-dict. Guards against the historical bug where new whitelisted
    keys were only accepted at the top-level body."""
    options = _submit_options_from_body(
        {
            "options": {
                "matrix": "BLOSUM80",
                "comp_based_stats": 1,
                "qcov_hsp_perc": 50,
            }
        }
    )

    assert options["matrix"] == "BLOSUM80"
    assert options["comp_based_stats"] == 1
    assert options["qcov_hsp_perc"] == 50
