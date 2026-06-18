"""Cross-surface convergence equivalence tests for the BLAST submit harness (#39).

Responsibility: Prove the AC "the same golden request through all three submit
surfaces (dashboard New Search, OpenAPI /v1/jobs, Service Bus drain) yields an
equivalent normalised PrecisionPlan", and that the searchsp/sharding policy is
identical across surfaces — server-derived calibrated searchsp on a clean
request, and the documented divergence (UI/OpenAPI reject a bad caller override
while the Service Bus bridge downgrades-with-trace) on a bad one.
Edit boundaries: Drive the shared `resolve_sharding_plan` exactly as each surface
does (UI/OpenAPI via `ExternalBlastSubmitRequest`; SB via its message body +
`allow_servicebus_downgrade=True`). No live Azure / Redis.
Key entry points: `test_clean_precise_request_converges_across_surfaces`,
`test_calibrated_searchsp_is_server_derived_on_all_surfaces`,
`test_bad_searchsp_override_rejected_on_api_downgraded_on_servicebus`.
Risky contracts: If a future change forks one surface onto a different
normaliser, these equivalence assertions must fail.
Validation: `uv run pytest -q api/tests/test_blast_submit_convergence.py`.
"""

from __future__ import annotations

from typing import Any

from api.routes.elastic_blast import ExternalBlastSubmitRequest
from api.services.blast.submit_payload import (
    _caller_supplied_searchsp,
    resolve_sharding_plan,
)
from api.services.web_blast_searchsp import default_for_database

# A real short nucleotide FASTA so the request model's query/taxonomy validator
# passes; core_nt is the only calibrated database, so the precise path resolves
# a verified search space.
GOLDEN_QUERY = ">conv-q1\n" + ("ACGTACGTACGTACGTACGTACGTACGTACGT" * 2) + "\n"
GOLDEN_DB = "core_nt"
PINNED_CORE_NT = default_for_database("core_nt").value  # 32156241807668


def _golden_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query_fasta": GOLDEN_QUERY,
        "db": GOLDEN_DB,
        "program": "blastn",
        "options": {
            "sharding_mode": "precise",
            "word_size": 28,
            "max_target_seqs": 500,
        },
    }
    body.update(overrides)
    return body


def _api_plan(body: dict[str, Any]):
    """Resolve the plan the way the dashboard New Search (`submit.py`) AND the
    OpenAPI `/v1/jobs` route (`elastic_blast.py`) both do — identical code via
    `ExternalBlastSubmitRequest`."""
    req = ExternalBlastSubmitRequest(**body)
    payload = req.model_dump(exclude_none=True)
    return resolve_sharding_plan(
        program=req.program,
        database=str(payload.get("db") or ""),
        options=payload.get("options"),
        caller_supplied_searchsp=req.options.db_effective_search_space,
    )


def _servicebus_plan(body: dict[str, Any]):
    """Resolve the plan the way the Service Bus drain (`servicebus/tasks.py`)
    does — same logical body, but `allow_servicebus_downgrade=True` and the
    caller searchsp pulled from the message body."""
    # The SB message body shape stays identical to /v1/jobs (the "consistent
    # with /v1/jobs" invariant), so it carries the same canonical options.
    payload = ExternalBlastSubmitRequest(**body).model_dump(exclude_none=True)
    return resolve_sharding_plan(
        program=str(payload.get("program") or "blastn"),
        database=str(payload.get("db") or ""),
        options=payload.get("options"),
        caller_supplied_searchsp=_caller_supplied_searchsp(body),
        allow_servicebus_downgrade=True,
    )


def test_clean_precise_request_converges_across_surfaces() -> None:
    """One golden precise request resolves to an EQUIVALENT PrecisionPlan on
    all three surfaces (the core #39 convergence check)."""
    body = _golden_body()
    ui = _api_plan(body)
    openapi = _api_plan(body)  # same route code as UI
    servicebus = _servicebus_plan(body)

    # The resolved options written identically into JobState + the Celery task
    # args (D1) must match across surfaces.
    assert ui.options == openapi.options == servicebus.options
    # The normalised precision report (precision_level / merge_strategy /
    # eligibility) must match.
    assert ui.precision == openapi.precision == servicebus.precision
    # A clean calibrated request is never downgraded and carries no errors.
    assert ui.downgraded is False and servicebus.downgraded is False
    assert ui.validation_errors == servicebus.validation_errors == []


def test_calibrated_searchsp_is_server_derived_on_all_surfaces() -> None:
    """searchsp is server-derived from the calibration table identically on all
    three surfaces (H-SH-1), even though the caller supplied none."""
    body = _golden_body()
    for plan in (_api_plan(body), _api_plan(body), _servicebus_plan(body)):
        assert plan.options.get("db_effective_search_space") == PINNED_CORE_NT


def test_bad_searchsp_override_rejected_on_api_downgraded_on_servicebus() -> None:
    """A bad caller `db_effective_search_space` must never be trusted blindly
    (H-SH-2): UI/OpenAPI reject it (validation error -> deterministic 422),
    while the Service Bus bridge downgrades-with-trace rather than dead-letter a
    recoverable job (D3 downgrade-with-trace)."""
    bad = 999_999_999  # not the calibrated core_nt value
    body = _golden_body(
        options={
            "sharding_mode": "precise",
            "word_size": 28,
            "max_target_seqs": 500,
            "db_effective_search_space": bad,
        }
    )

    api = _api_plan(body)
    sb = _servicebus_plan(body)

    # API surfaces (UI + OpenAPI) surface a blocking validation error so the
    # route turns it into a deterministic 422 (`_validate_submit_contracts` flips
    # eligible=False). The resolver leaves the raw value in options but never
    # treats it as accepted, and it does NOT downgrade — it blocks.
    assert api.validation_errors, "API path must reject a bad searchsp override"
    assert api.downgraded is False

    # Service Bus downgrades-with-trace instead of blocking: no blocking error,
    # flagged downgraded, and the bad calibrated value is dropped (BLAST
    # computes its own) so a recoverable job is never dead-lettered.
    assert sb.downgraded is True
    assert sb.validation_errors == []
    assert sb.options.get("db_effective_search_space") != bad
