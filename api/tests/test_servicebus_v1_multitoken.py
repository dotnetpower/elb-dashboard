"""Tests for the Service Bus multi-token (``/v1/jobs``) submit path.

Responsibility: Verify a message carrying ``blast_options`` is routed to the
    sibling ``/v1/jobs`` (free-form, multi-token ``outfmt``) instead of the
    XML-locked ``/api/v1/elastic-blast/submit``, that the validation model
    accepts a merge-compatible tabular layout and rejects an incompatible one,
    and that server-derived metadata (source, sharded-DB profile) is stamped.
Edit boundaries: Pure payload-shaping + routing behaviour; no live sibling.
Key entry points: ``ExternalBlastV1Request``, ``_is_v1_jobs_message``,
    ``_build_v1_jobs_payload``, ``external_blast.submit_job_v1``.
Risky contracts: a tabular outfmt missing evalue/bitscore must be rejected at
    submit time (the shard merge re-ranks by those columns).
Validation: ``uv run pytest -q api/tests/test_servicebus_v1_multitoken.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

_FASTA = (
    ">NC_003310.1 Monkeypox\n"
    "ATGGAGAAGCGAGAAGTTAATAAAGCTCTGTATGATCTTCAACGTAGTACTATGGTGTAC\n"
)

_USER_BODY = {
    "program": "blastn",
    "db": "core_nt",
    "query_fasta": _FASTA,
    "blast_options": {
        "evalue": 0.05,
        "max_target_seqs": 100,
        "outfmt": "7 std staxids sstrand qseq sseq",
        "extra": "-word_size 28 -dust yes -soft_masking false -searchsp 32156241807668",
    },
    "resource_profile": "core_nt_safe",
}


# --------------------------------------------------------------------------- #
# ExternalBlastV1Request validation
# --------------------------------------------------------------------------- #


def test_v1_request_accepts_multitoken_std_outfmt() -> None:
    from api.routes.elastic_blast import ExternalBlastV1Request

    req = ExternalBlastV1Request(**{**_USER_BODY, "external_correlation_id": "corr-1"})
    assert req.blast_options.outfmt == "7 std staxids sstrand qseq sseq"
    assert req.blast_options.extra and "-searchsp" in req.blast_options.extra
    assert req.db == "core_nt"


def test_v1_request_rejects_tabular_without_evalue_bitscore() -> None:
    """A tabular layout the shard merge cannot re-rank is rejected at submit."""
    from api.routes.elastic_blast import ExternalBlastV1Request

    bad = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": _FASTA,
        "blast_options": {"outfmt": "7 qseqid sseqid staxids"},
    }
    with pytest.raises(ValidationError, match="shard-merge compatible"):
        ExternalBlastV1Request(**bad)


def test_v1_request_accepts_xml_outfmt() -> None:
    from api.routes.elastic_blast import ExternalBlastV1Request

    req = ExternalBlastV1Request(
        program="blastn", db="core_nt", query_fasta=_FASTA, blast_options={"outfmt": "5"}
    )
    assert req.blast_options.outfmt == "5"


def test_v1_request_rejects_db_path_traversal() -> None:
    from api.routes.elastic_blast import ExternalBlastV1Request

    with pytest.raises(ValidationError, match=r"\.\."):
        ExternalBlastV1Request(program="blastn", db="a/../b", query_fasta=_FASTA)


# --------------------------------------------------------------------------- #
# Routing detection + payload build
# --------------------------------------------------------------------------- #


def test_is_v1_jobs_message_detects_blast_options() -> None:
    from api.tasks.servicebus import tasks as sb

    assert sb._is_v1_jobs_message({"blast_options": {"outfmt": "7 std"}}) is True
    # The XML-locked `options` object stays on the legacy path.
    assert sb._is_v1_jobs_message({"options": {"outfmt": 5}}) is False
    assert sb._is_v1_jobs_message({}) is False


def _msg(body: dict) -> object:
    from api.services.service_bus import ParsedMessage

    return ParsedMessage(
        body=body,
        raw_body="",
        message_id="m1",
        correlation_id=str(body.get("external_correlation_id") or "corr-x"),
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=1,
    )


def test_build_v1_payload_preserves_multitoken_and_stamps_metadata() -> None:
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    payload = sb._build_v1_jobs_payload(
        _msg({**_USER_BODY, "external_correlation_id": "corr-1"}), ServiceBusConfig()
    )
    assert payload is not None
    # Multi-token outfmt + extra survive end-to-end.
    assert payload["blast_options"]["outfmt"] == "7 std staxids sstrand qseq sseq"
    assert "-searchsp" in payload["blast_options"]["extra"]
    # The sibling /v1/jobs only accepts {dashboard, external_api, terminal,
    # system}; "servicebus" 400s. We send external_api on the wire while the
    # dashboard tracking row (written elsewhere) stays "servicebus".
    assert payload["submission_source"] == "external_api"
    assert payload["external_correlation_id"] == "corr-1"
    # Explicit sharding profile preserved.
    assert payload["resource_profile"] == "core_nt_safe"


def test_build_v1_payload_uses_sibling_accepted_source() -> None:
    """The /v1/jobs payload must carry a submission_source the sibling accepts
    (external_api), never 'servicebus' which the sibling 400s. A producer cannot
    spoof it either."""
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    spoofed = sb._build_v1_jobs_payload(
        _msg(
            {
                **_USER_BODY,
                "submission_source": "servicebus",
                "external_correlation_id": "corr-spoof",
            }
        ),
        ServiceBusConfig(),
    )
    assert spoofed is not None
    assert spoofed["submission_source"] == "external_api"


def test_build_v1_payload_promotes_core_nt_profile_when_missing() -> None:
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    body = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": _FASTA,
        "blast_options": {"outfmt": "7 std staxids"},
        "external_correlation_id": "corr-2",
    }
    payload = sb._build_v1_jobs_payload(_msg(body), ServiceBusConfig())
    assert payload is not None
    assert payload["resource_profile"] == "core_nt_safe"  # promoted


def test_build_v1_payload_accepts_external_queue_body_without_internal_metadata() -> None:
    """External producers do not need to know dashboard-only submit metadata."""
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    body = {
        "request_id": "req-64f6f5cd-7f53-4bb7-b8d8-7181198d2089",
        "type": "blast.request",
        "query_fasta": _FASTA,
        "program": "blastn",
        "db": "core_nt",
        "blast_options": {
            "evalue": 0.05,
            "max_target_seqs": 100,
            "outfmt": (
                "7 qseqid sacc staxid ssciname pident length evalue bitscore "
                "qstart qend sstart send qcovhsp sseq"
            ),
            "extra": "-word_size 28 -dust yes -soft_masking false -searchsp 32156241807668",
        },
        "taxid": 10244,
        "is_inclusive": True,
    }

    payload = sb._build_v1_jobs_payload(_msg(body), ServiceBusConfig())

    assert payload is not None
    assert payload["external_correlation_id"] == "corr-x"
    assert payload["submission_source"] == "external_api"
    assert payload["resource_profile"] == "core_nt_safe"
    assert payload["taxid"] == 10244
    assert payload["is_inclusive"] is True
    assert payload["blast_options"] == body["blast_options"]
    assert "request_id" not in payload
    assert "type" not in payload


def test_build_v1_payload_rejects_incompatible_outfmt() -> None:
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    body = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": _FASTA,
        "blast_options": {"outfmt": "7 qseqid sseqid"},
        "external_correlation_id": "corr-3",
    }
    # Validation failure → None so the drain dead-letters it.
    assert sb._build_v1_jobs_payload(_msg(body), ServiceBusConfig()) is None


# --------------------------------------------------------------------------- #
# Web BLAST search-space (searchsp) parity on the /v1/jobs path
# --------------------------------------------------------------------------- #

# core_nt's calibrated Web BLAST effective search space (api/services/
# web_blast_searchsp.py WEB_BLAST_SEARCHSP_DEFAULTS["core_nt"].value).
_CORE_NT_SEARCHSP = 32_156_241_807_668


def _v1_no_searchsp_body(**overrides: object) -> dict:
    """A v1 body whose blast_options carry NO -searchsp (so the oracle applies)."""
    body = {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": _FASTA,
        "blast_options": {
            "outfmt": "7 std staxids sstrand qseq sseq",
            "extra": "-word_size 28 -dust yes",
        },
        "external_correlation_id": "corr-ss",
    }
    body.update(overrides)
    return body


def test_build_v1_payload_injects_oracle_searchsp_when_absent() -> None:
    """An outfmt-7 SB submit with no -searchsp gets the SAME calibrated value the
    XML path / New Search apply, instead of relying on the sibling's fixed
    default."""
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    payload = sb._build_v1_jobs_payload(_msg(_v1_no_searchsp_body()), ServiceBusConfig())
    assert payload is not None
    extra = payload["blast_options"]["extra"]
    assert f"-searchsp {_CORE_NT_SEARCHSP}" in extra
    # The pre-existing flags are preserved alongside the injected searchsp.
    assert "-word_size 28" in extra and "-dust yes" in extra


def test_build_v1_payload_honors_structured_db_effective_search_space() -> None:
    """A caller may pass the oracle value via the structured
    db_effective_search_space field (mirroring the XML path); it is applied as a
    -searchsp flag and never leaks to the sibling wire payload."""
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    body = _v1_no_searchsp_body()
    body["blast_options"] = {
        "outfmt": "7 std staxids sstrand qseq sseq",
        "db_effective_search_space": _CORE_NT_SEARCHSP,
    }
    payload = sb._build_v1_jobs_payload(_msg(body), ServiceBusConfig())
    assert payload is not None
    assert f"-searchsp {_CORE_NT_SEARCHSP}" in payload["blast_options"]["extra"]
    # The convenience field is NOT a sibling /v1/jobs field — it must be stripped.
    assert "db_effective_search_space" not in payload["blast_options"]


def test_build_v1_payload_does_not_override_caller_pinned_searchsp() -> None:
    """A caller who pins -searchsp themselves keeps exactly that value (the oracle
    never double-injects or overrides it)."""
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    body = _v1_no_searchsp_body()
    body["blast_options"] = {
        "outfmt": "7 std staxids sstrand qseq sseq",
        "extra": "-searchsp 999",
    }
    payload = sb._build_v1_jobs_payload(_msg(body), ServiceBusConfig())
    assert payload is not None
    extra = payload["blast_options"]["extra"]
    assert "-searchsp 999" in extra
    # Exactly one -searchsp token (no double-injection of the oracle value).
    assert extra.count("-searchsp") == 1
    assert str(_CORE_NT_SEARCHSP) not in extra


def test_build_v1_payload_no_searchsp_for_uncalibrated_db() -> None:
    """An uncalibrated database has no dashboard oracle value, so we inject
    nothing and leave the sibling to apply its own default (no false parity)."""
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    body = _v1_no_searchsp_body(db="16S_ribosomal_RNA")
    payload = sb._build_v1_jobs_payload(_msg(body), ServiceBusConfig())
    assert payload is not None
    extra = payload["blast_options"]["extra"]
    assert "-searchsp" not in extra
    # The caller's own flags are still preserved untouched.
    assert "-word_size 28" in extra and "-dust yes" in extra


def test_build_v1_payload_searchsp_resolution_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug/exception in searchsp resolution must NEVER fail an otherwise-valid
    submit; the payload is produced and the sibling applies its own default."""
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    def _boom(**_kwargs: object) -> object:
        raise RuntimeError("searchsp resolver exploded")

    monkeypatch.setattr(
        "api.services.blast.submit_payload.resolve_sharding_plan", _boom
    )
    payload = sb._build_v1_jobs_payload(_msg(_v1_no_searchsp_body()), ServiceBusConfig())
    assert payload is not None
    # No injection on failure, but the multi-token submit still goes through.
    assert "-searchsp" not in payload["blast_options"]["extra"]
    assert payload["blast_options"]["outfmt"] == "7 std staxids sstrand qseq sseq"


# --------------------------------------------------------------------------- #
# submit_job_v1 posts to /v1/jobs
# --------------------------------------------------------------------------- #


def test_submit_job_v1_posts_to_v1_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import external_blast

    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"job_id": "v1job123", "status": "queued"}

    class _Client:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def post(self, path: str, **_k: object) -> _Resp:
            captured["path"] = path
            return _Resp()

    monkeypatch.setattr(external_blast.httpx, "Client", _Client)
    monkeypatch.setattr(external_blast, "_base_url", lambda *a, **k: "http://sib")
    monkeypatch.setattr(external_blast, "_headers", lambda **k: {"Accept": "application/json"})

    out = external_blast.submit_job_v1({"db": "core_nt", "idempotency_key": "k1"})
    assert out["job_id"] == "v1job123"
    assert captured["path"] == "/v1/jobs"
