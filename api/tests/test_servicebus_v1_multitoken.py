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
