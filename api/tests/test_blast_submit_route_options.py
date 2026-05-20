from __future__ import annotations

from api.routes._blast_shared import (
    _apply_web_blast_searchsp_default,
    _normalise_blast_submit_body,
    _submit_options_from_body,
)


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
