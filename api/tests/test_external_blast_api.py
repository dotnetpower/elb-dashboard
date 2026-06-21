"""Tests for External BLAST API behavior.

Responsibility: Tests for External BLAST API behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_external_blast_submit_forwards_contract`,
`test_canonical_jobs_external_submit_uses_trusted_metadata`,
`test_external_blast_events_falls_back_to_current_status`,
`test_external_blast_manifest_maps_result_files`, `test_external_blast_rejects_non_xml_outfmt`,
`test_external_blast_rejects_invalid_program`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_external_blast_api.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_external_blast_submit_forwards_contract(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured = {}

    def fake_submit(payload):
        captured.update(payload)
        return {
            "job_id": "aaaaaaaaaaaa",
            "status": "queued",
            "created_at": "2026-05-12T10:00:00Z",
            "blast_version": "2.17.0+",
            "db_name": "core_nt",
            "db_version": "2026-05-02",
        }

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
            "taxid": 3431483,
            "is_inclusive": False,
            "options": {
                "outfmt": 5,
                "word_size": 28,
                "dust": True,
                "evalue": 0.05,
                "max_target_seqs": 500,
            },
            "batch_len": 462,
            "idempotency_key": "req-1",
            "submission_source": "dashboard",
            "external_correlation_id": "caller-supplied",
        },
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == "aaaaaaaaaaaa"
    assert response.json()["status"] == "queued"
    assert response.json()["submission_source"] == "external_api"
    assert response.json()["external_correlation_id"] == "caller-supplied"
    assert captured["submission_source"] == "external_api"
    assert captured["external_correlation_id"] == "caller-supplied"
    assert captured["idempotency_key"] == "req-1"
    assert captured["canonical_request"]["metadata"]["submission_source"] == "external_api"
    assert captured["compatibility_contract"]["mode"] == "precise"
    assert captured["provenance"]["compatibility"]["mode"] == "precise"
    assert captured["taxid"] == 3431483
    assert captured["is_inclusive"] is False
    assert captured["options"]["outfmt"] == 5
    assert captured["batch_len"] == 462
    assert "caller_oid" not in captured


def test_external_blast_submit_threads_openapi_scope(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured: dict[str, Any] = {}

    def fake_ready(**kwargs: Any) -> dict[str, Any]:
        captured["ready"] = dict(kwargs)
        return {"ready": True}

    def fake_submit(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        captured["payload"] = dict(payload)
        captured["submit"] = dict(kwargs)
        return {"job_id": "aaaaaaaaaaaa", "status": "queued"}

    monkeypatch.setattr(external_blast, "ready", fake_ready)
    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
            "subscription_id": "sub-a",
            "resource_group": "rg-a",
            "cluster_name": "aks-a",
        },
    )

    assert response.status_code == 202
    assert captured["ready"] == {
        "subscription_id": "sub-a",
        "resource_group": "rg-a",
        "cluster_name": "aks-a",
    }
    assert captured["submit"] == {
        "subscription_id": "sub-a",
        "resource_group": "rg-a",
        "cluster_name": "aks-a",
    }
    assert captured["payload"]["subscription_id"] == "sub-a"
    assert captured["payload"]["resource_group"] == "rg-a"
    assert captured["payload"]["cluster_name"] == "aks-a"


# --------------------------------------------------------------------------- #
# Token self-heal on a stale-token 401 (job-detail recovery / sync / submit).
# --------------------------------------------------------------------------- #


def test_request_with_token_resync_retries_once_on_401(monkeypatch):
    """A 401 triggers exactly one token resync + retry with the healed token."""
    from api.services import external_blast

    monkeypatch.setattr(external_blast, "_resync_token_after_401", lambda: "healed-token")
    calls: list[int] = []

    def send(_client):
        calls.append(1)
        return SimpleNamespace(status_code=401 if len(calls) == 1 else 200)

    resp = external_blast._request_with_token_resync(
        base_url="http://sibling.invalid",
        timeout=5.0,
        api_token=None,
        subscription_id="",
        resource_group="",
        cluster_name="",
        send=send,
        label="get_job",
    )
    assert resp.status_code == 200
    assert len(calls) == 2  # original + one retry


def test_request_with_token_resync_surfaces_401_when_no_token(monkeypatch):
    """When no live token can be recovered the original 401 is returned (no retry)."""
    from api.services import external_blast

    monkeypatch.setattr(external_blast, "_resync_token_after_401", lambda: "")
    calls: list[int] = []

    def send(_client):
        calls.append(1)
        return SimpleNamespace(status_code=401)

    resp = external_blast._request_with_token_resync(
        base_url="http://sibling.invalid",
        timeout=5.0,
        api_token=None,
        subscription_id="",
        resource_group="",
        cluster_name="",
        send=send,
        label="get_job",
    )
    assert resp.status_code == 401
    assert len(calls) == 1  # no retry without a recovered token


def test_request_with_token_resync_passthrough_on_success(monkeypatch):
    """A non-401 response returns immediately and never attempts a resync."""
    from api.services import external_blast

    resynced: list[int] = []
    monkeypatch.setattr(
        external_blast,
        "_resync_token_after_401",
        lambda: resynced.append(1) or "x",
    )

    resp = external_blast._request_with_token_resync(
        base_url="http://sibling.invalid",
        timeout=5.0,
        api_token="tok",
        subscription_id="",
        resource_group="",
        cluster_name="",
        send=lambda _client: SimpleNamespace(status_code=200),
        label="list_jobs",
    )
    assert resp.status_code == 200
    assert resynced == []  # resync never attempted on success


def test_stream_file_resyncs_token_on_401(monkeypatch):
    """A stale-token 401 on the file download self-heals: resync + retry once, then stream.

    Reproduces the Service-Bus completion ``download_url`` failure after a
    control-plane redeploy / cluster restart — ``stream_file`` must recover the
    same way every other sibling call (``_request_with_token_resync``) does.
    """
    from api.services import external_blast

    monkeypatch.setattr(external_blast, "_base_url", lambda *a, **k: "http://sibling.invalid")
    monkeypatch.setattr(
        external_blast,
        "_headers",
        lambda **k: {"X-ELB-API-Token": (k.get("api_token") or "")},
    )
    monkeypatch.setattr(external_blast, "_resync_token_after_401", lambda: "healed-token")

    calls = {"n": 0, "tokens": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["tokens"].append(request.headers.get("x-elb-api-token", ""))
        if calls["n"] == 1:
            return httpx.Response(401, json={"detail": "missing or invalid X-ELB-API-Token"})
        return httpx.Response(
            200,
            content=b"BLAST-XML-RESULT-BYTES",
            headers={
                "content-type": "application/gzip",
                "content-disposition": 'attachment; filename="r.out.gz"',
            },
        )

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(external_blast.httpx, "Client", client_factory)

    out = external_blast.stream_file("job123", "result-001", api_token=None)
    body = b"".join(out.chunks)
    assert body == b"BLAST-XML-RESULT-BYTES"
    assert calls["n"] == 2  # original 401 + one retry
    assert calls["tokens"][1] == "healed-token"  # retry carried the recovered token
    assert out.filename == "r.out.gz"


def test_stream_file_surfaces_401_when_no_token_recovered(monkeypatch):
    """When no live token can be recovered, the file download surfaces the 401."""
    from api.services import external_blast

    monkeypatch.setattr(external_blast, "_base_url", lambda *a, **k: "http://sibling.invalid")
    monkeypatch.setattr(external_blast, "_headers", lambda **k: {})
    monkeypatch.setattr(external_blast, "_resync_token_after_401", lambda: "")

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "missing or invalid X-ELB-API-Token"})

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(external_blast.httpx, "Client", client_factory)

    with pytest.raises(HTTPException) as raised:
        external_blast.stream_file("job123", "result-001", api_token=None)
    assert raised.value.status_code == 401
    # original + one reopen with the original token (no healed token to retry with)
    assert calls["n"] == 2


def test_stream_file_unreachable_openapi_returns_503(monkeypatch):
    """A download against a stopped cluster surfaces a clean 503, not a 500.

    Reproduces the Service-Bus completion ``download_url`` being followed after
    the AKS cluster auto-stopped: the elb-openapi pod is gone, so the very first
    ``client.send`` raises ``httpx.ConnectError`` inside ``_open`` before the
    outer ``client`` is bound. The fix closes the just-created client and leaves
    ``client = None`` so the ``except`` block raises ``HTTPException(503,
    openapi_unreachable)`` instead of ``UnboundLocalError`` (a 500) — and never
    leaks the connection pool.
    """
    from api.services import external_blast

    monkeypatch.setattr(external_blast, "_base_url", lambda *a, **k: "http://sibling.invalid")
    monkeypatch.setattr(external_blast, "_headers", lambda **k: {})

    closed = {"n": 0}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        client = real_client(*args, **kwargs)
        real_close = client.close

        def counting_close() -> None:
            closed["n"] += 1
            real_close()

        client.close = counting_close  # type: ignore[method-assign]
        return client

    monkeypatch.setattr(external_blast.httpx, "Client", client_factory)

    with pytest.raises(HTTPException) as raised:
        external_blast.stream_file("job123", "result-001", api_token=None)
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "openapi_unreachable"
    # The client created inside _open must be closed (no connection-pool leak).
    assert closed["n"] >= 1



def test_token_resync_coalesces_concurrent_callers(monkeypatch):
    """A burst of 401s must NOT each fire an independent cluster read — the
    coalescing cache serves the just-recovered token to the queued callers."""
    from api.services import external_blast

    external_blast.reset_token_resync_cache()
    calls: list[int] = []

    def _fake_cluster_resync():
        calls.append(1)
        return "healed-token"

    monkeypatch.setattr(
        "api.services.openapi.token.resync_openapi_api_token_from_cluster",
        _fake_cluster_resync,
    )
    try:
        # Three callers within the coalesce TTL → exactly one cluster read.
        tokens = [external_blast._resync_token_after_401() for _ in range(3)]
        assert tokens == ["healed-token", "healed-token", "healed-token"]
        assert len(calls) == 1, "expected a single coalesced cluster read"
    finally:
        external_blast.reset_token_resync_cache()


def test_token_resync_empty_result_not_cached(monkeypatch):
    """An empty recovery must not be cached — the next 401 retries the read."""
    from api.services import external_blast

    external_blast.reset_token_resync_cache()
    calls: list[int] = []

    def _fake_empty():
        calls.append(1)
        return ""

    monkeypatch.setattr(
        "api.services.openapi.token.resync_openapi_api_token_from_cluster",
        _fake_empty,
    )
    try:
        external_blast._resync_token_after_401()
        external_blast._resync_token_after_401()
        assert len(calls) == 2, "empty result must not suppress the next retry"
    finally:
        external_blast.reset_token_resync_cache()


def test_external_blast_options_default_evalue_matches_ncbi(monkeypatch):
    """Omitting `options` must default evalue to 0.05 (NCBI megablast default)."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured: dict = {}

    def fake_submit(payload):
        captured.update(payload)
        return {
            "job_id": "bbbbbbbbbbbb",
            "status": "queued",
            "created_at": "2026-05-12T10:00:00Z",
            "blast_version": "2.17.0+",
            "db_name": "core_nt",
            "db_version": "2026-05-02",
        }

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
        },
    )

    assert response.status_code == 202
    assert captured["options"]["evalue"] == 0.05

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured = {}

    def fake_submit(payload):
        captured.update(payload)
        return {"job_id": "aaaaaaaaaaaa", "status": "queued"}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    client = TestClient(app)

    response = client.post(
        "/api/blast/jobs",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
            "idempotency_key": "req-2",
            "submission_source": "dashboard",
        },
    )

    assert response.status_code == 202
    assert captured["submission_source"] == "external_api"
    assert captured["idempotency_key"] == "req-2"
    assert captured["external_correlation_id"]
    assert captured["canonical_request"]["query"]["kind"] == "inline_fasta"
    assert captured["compatibility_contract"]["mode"] == "precise"
    assert captured["provenance"]["query"]["sha256"]


def test_external_blast_submit_derives_precise_searchsp(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured: dict[str, object] = {}

    def fake_submit(payload):
        captured.update(payload)
        return {"job_id": "precise123", "status": "queued"}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
            "options": {"sharding_mode": "precise"},
        },
    )

    assert response.status_code == 202
    assert captured["options"]["sharding_mode"] == "precise"
    assert captured["options"]["db_effective_search_space"] == 32_156_241_807_668


def test_external_blast_submit_rejects_bad_searchsp_override(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
            "options": {
                "sharding_mode": "precise",
                "db_effective_search_space": 42,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "web_blast_compatibility_blocked"


def test_external_blast_events_falls_back_to_current_status(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {"job_id": job_id, "status": "running", "updated_at": "now"},
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/events")

    assert response.status_code == 200
    assert response.json()["events"][0]["phase"] == "running"


def test_external_blast_job_routes_thread_scope_query(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    calls: list[dict[str, Any]] = []

    def fake_get_job(job_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"job_id": job_id, **kwargs})
        return {"job_id": job_id, "status": "running", "result": {"files": []}}

    monkeypatch.setattr(external_blast, "get_job", fake_get_job)
    client = TestClient(app)
    params = {
        "subscription_id": "sub-a",
        "resource_group": "rg-a",
        "cluster_name": "aks-a",
    }

    assert client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa", params=params).status_code == 200
    assert (
        client.get(
            "/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/events",
            params=params,
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/manifest", params=params).status_code
        == 200
    )

    assert len(calls) == 3
    for call in calls:
        assert call["subscription_id"] == "sub-a"
        assert call["resource_group"] == "rg-a"
        assert call["cluster_name"] == "aks-a"


def test_external_blast_manifest_maps_result_files(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda _job_id: {
            "result": {
                "files": [
                    {
                        "file_id": "result-xml",
                        "filename": "blast_result.xml",
                        "format": "blast_xml",
                        "size_bytes": 42,
                    }
                ]
            }
        },
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/manifest")

    assert response.status_code == 200
    assert response.json()["parseable_count"] == 1


def test_external_blast_rejects_non_xml_outfmt(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "options": {"outfmt": 6}},
    )

    assert response.status_code == 422


def test_external_blast_accepts_string_outfmt_five():
    """The OpenAPI /v1/jobs examples (and the dashboard's own API Reference
    spec) document ``outfmt`` as the JSON string ``"5"``. A producer copying
    that verbatim must be accepted, not rejected — on the Service Bus path a
    rejection means a dead-letter. The string form is coerced to int 5."""
    from api.routes.elastic_blast import ExternalBlastOptions

    assert ExternalBlastOptions(outfmt="5").outfmt == 5
    assert ExternalBlastOptions(outfmt=5).outfmt == 5
    # Non-XML formats still fail regardless of string/int form.
    for bad in ("6", 6, "7", "xml"):
        with pytest.raises(ValidationError):
            ExternalBlastOptions(outfmt=bad)


def test_external_blast_submit_accepts_string_outfmt_five(monkeypatch):
    """End-to-end: a submit body using the documented string ``outfmt: "5"``
    is accepted (HTTP 202), mirroring the OpenAPI /v1/jobs contract."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured: dict[str, object] = {}

    def _fake_submit(payload, **_kwargs):
        captured.update(payload)
        return {"job_id": "ext-strfmt", "status": "queued"}

    monkeypatch.setattr(external_blast, "submit_job", _fake_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGC",
            "db": "core_nt",
            "options": {"outfmt": "5"},
        },
    )

    assert response.status_code == 202
    assert captured["options"]["outfmt"] == 5


def test_external_blast_rejects_invalid_program(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "program": "rm -rf"},
    )

    assert response.status_code == 422


def test_external_blast_rejects_path_traversal_db():
    """A ``..`` path segment in the db name is rejected at the boundary (422),
    deterministically, instead of depending on upstream reachability."""
    from api.routes.elastic_blast import ExternalBlastSubmitRequest
    from pydantic import ValidationError

    for bad in ("../../etc/passwd", "../core_nt", "a/../b", ".."):
        with pytest.raises(ValidationError):
            ExternalBlastSubmitRequest(query_fasta=">q\nACGT", db=bad)
    # A legitimate sharded-prefix db with '/' and '.' is still allowed.
    ok = ExternalBlastSubmitRequest(query_fasta=">q\nACGT", db="10shards/core_nt_shard_.v5")
    assert ok.db == "10shards/core_nt_shard_.v5"


def test_external_blast_rejects_non_positive_taxid(monkeypatch):
    """NCBI taxonomy ids are positive integers; 0 / negative must be rejected at
    the boundary (not forwarded as a nonsensical organism filter)."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    for bad in (-5, 0):
        response = client.post(
            "/api/v1/elastic-blast/submit",
            json={"query_fasta": ">q1\nATGC", "db": "core_nt", "taxid": bad, "is_inclusive": True},
        )
        assert response.status_code == 422, f"taxid={bad} should be rejected"
    # A valid positive taxid still passes validation (model-level).
    from api.routes.elastic_blast import ExternalBlastSubmitRequest

    ok = ExternalBlastSubmitRequest(query_fasta=">q\nACGT", db="core_nt", taxid=562)
    assert ok.taxid == 562


def test_external_blast_rejects_invalid_fasta(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": "ATGC", "db": "core_nt"},
    )

    assert response.status_code == 422


def test_external_blast_defaults_taxid_to_inclusive(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured = {}

    def fake_submit(payload):
        captured.update(payload)
        return {"job_id": "aaaaaaaaaaaa", "status": "queued"}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "taxid": 3431483},
    )

    assert response.status_code == 202
    assert captured["is_inclusive"] is True


def test_external_blast_rejects_inclusive_without_taxid(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "is_inclusive": True},
    )

    assert response.status_code == 422


def test_external_blast_rejects_string_priority(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "priority": "urgent"},
    )

    assert response.status_code == 422


def test_upstream_error_detail_is_sanitised() -> None:
    from api.services import external_blast

    detail = external_blast._sanitise_detail(
        {"message": "failed ?sig=a&sp=b&se=c Bearer abcdefghijklmnopqrstuvwxyz1234567890"}
    )

    assert "sig=a" not in detail["message"]
    assert "Bearer abc" not in detail["message"]


def test_streaming_upstream_error_detail_is_read_and_sanitised() -> None:
    from api.services import external_blast

    request = httpx.Request("GET", "https://example.test/result")
    response = httpx.Response(
        500,
        request=request,
        stream=httpx.ByteStream(b'{"message":"failed ?sig=secret-token"}'),
    )
    exc = httpx.HTTPStatusError("boom", request=request, response=response)

    with pytest.raises(HTTPException) as raised:
        external_blast._raise_upstream_error(exc)

    assert raised.value.status_code == 500
    assert raised.value.detail["message"] == "failed ?sig=<redacted>"


def test_upstream_error_logs_sanitised_response_detail(caplog: pytest.LogCaptureFixture) -> None:
    from api.services import external_blast

    caplog.set_level("WARNING", logger="api.services.external_blast")
    request = httpx.Request("GET", "https://example.test/api/v1/elastic-blast/jobs/job-1")
    response = httpx.Response(
        400,
        request=request,
        json={
            "message": "bad request Bearer abcdefghijklmnopqrstuvwxyz1234567890",
        },
    )
    exc = httpx.HTTPStatusError("boom", request=request, response=response)

    with pytest.raises(HTTPException):
        external_blast._raise_upstream_error(exc)

    text = caplog.text
    assert "OpenAPI upstream request failed" in text
    assert "status=400" in text
    assert "https://example.test/api/v1/elastic-blast/jobs/job-1" in text
    assert "bad request" in text
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "Bearer <redacted>" in text


def test_upstream_filename_is_sanitised() -> None:
    from api.services import external_blast

    assert external_blast._safe_filename('"bad\r\nX-Test: 1.xml"') == "bad__X-Test__1.xml"


def test_external_blast_status_forwards(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "running",
            "progress_pct": 45,
            "created_at": "2026-05-12T10:00:00Z",
        },
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa")

    assert response.status_code == 200
    assert response.json()["progress_pct"] == 45


def test_external_blast_status_normalises_contract(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "completed",
            "db": "core_nt",
            "result": {
                "files": [
                    {
                        "file_id": "result-001",
                        "name": "batch_001.xml.gz",
                        "size": 123,
                    }
                ]
            },
        },
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["db_name"] == "core_nt"
    assert body["result"]["files"][0] == {
        "file_id": "result-001",
        "filename": "batch_001.xml.gz",
        "name": "batch_001.xml.gz",
        "format": "blast_xml",
        "size_bytes": 123,
        "size": 123,
    }


def test_external_blast_submit_backfills_empty_upstream_metadata(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "submit_job",
        lambda _payload: {
            "job_id": "aaaaaaaaaaaa",
            "status": "accepted",
            "submission_source": None,
            "external_correlation_id": "",
        },
    )
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGC",
            "db": "core_nt",
            "external_correlation_id": "notebook-run-42",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["submission_source"] == "external_api"
    assert body["external_correlation_id"] == "notebook-run-42"


def test_external_blast_rejects_unsafe_job_id(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.get("/api/v1/elastic-blast/jobs/bad$id")

    assert response.status_code == 422


def test_external_blast_rejects_non_hex_job_id(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.get("/api/v1/elastic-blast/jobs/ABC123")

    assert response.status_code == 422


def test_path_segment_is_percent_encoded() -> None:
    from api.services import external_blast

    assert external_blast._path_segment("bad/id") == "bad%2Fid"


def test_external_blast_base_url_uses_runtime_cache(monkeypatch) -> None:
    monkeypatch.delenv("ELB_OPENAPI_BASE_URL", raising=False)
    from api.services import external_blast
    from api.services.openapi import runtime as openapi_runtime

    monkeypatch.setattr(openapi_runtime, "get_openapi_base_url", lambda: "http://10.0.0.4")

    assert external_blast._base_url() == "http://10.0.0.4"


def test_external_blast_headers_include_api_and_internal_tokens(monkeypatch) -> None:
    from api.services import external_blast

    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")
    monkeypatch.setenv("ELB_OPENAPI_INTERNAL_TOKEN", "internal-token")

    assert external_blast._headers() == {
        "Accept": "application/json",
        "X-ELB-API-Token": "api-token",
        "X-ELB-Internal-Token": "internal-token",
    }


def test_external_blast_list_uses_short_timeout(monkeypatch) -> None:
    from api.services import external_blast

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _path: str) -> object:
            raise httpx.ReadTimeout("slow list endpoint")

    monkeypatch.setattr(external_blast.httpx, "Client", FakeClient)

    with pytest.raises(HTTPException) as raised:
        external_blast.list_jobs(base_url="http://openapi")

    assert captured["timeout"] == external_blast._LIST_TIMEOUT_SECONDS
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "openapi_unreachable"


def test_external_blast_facade_list_is_cached(monkeypatch) -> None:
    """The direct facade ``GET /api/v1/elastic-blast/jobs`` MUST serve repeat
    polls from the shared external-jobs cache rather than hitting the sibling
    on every request (issue #30 — the facade previously had no cache, so a
    stale/unreachable base URL cost ``_LIST_TIMEOUT_SECONDS`` then 503 on each
    poll while the combined ``/api/blast/jobs`` route felt faster).
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    hits = {"count": 0}

    def list_jobs(**_kwargs):
        hits["count"] += 1
        return {"jobs": [{"job_id": "facade-1"}], "count": 1}

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    client = TestClient(app)

    first = client.get("/api/v1/elastic-blast/jobs")
    second = client.get("/api/v1/elastic-blast/jobs")

    assert first.status_code == 200
    assert first.json() == {"jobs": [{"job_id": "facade-1"}], "count": 1}
    assert second.json() == first.json()
    # Both polls share the TTL cache → exactly one upstream round-trip.
    assert hits["count"] == 1


def test_external_blast_facade_list_caches_upstream_failure(monkeypatch) -> None:
    """A failing sibling MUST be negatively cached so SPA/API polling does not
    keep paying the upstream round-trip to learn the same failure again.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    hits = {"count": 0}

    def list_jobs(**_kwargs):
        hits["count"] += 1
        raise HTTPException(
            503,
            detail={"code": "openapi_unreachable", "message": "stale base url"},
        )

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    client = TestClient(app)

    first = client.get("/api/v1/elastic-blast/jobs")
    second = client.get("/api/v1/elastic-blast/jobs")

    assert first.status_code == 503
    assert second.status_code == 503
    assert first.json()["code"] == "openapi_unreachable"
    # Negative cache short-circuits the repeat poll.
    assert hits["count"] == 1


def test_external_blast_delete_job_calls_v1_endpoint(monkeypatch) -> None:
    from api.services import external_blast

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"job_id": "abc123", "status": "deleted"})

    transport = httpx.MockTransport(handler)
    original_client_cls = httpx.Client

    class _StubClient(original_client_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(external_blast.httpx, "Client", _StubClient)

    result = external_blast.delete_job("abc123", base_url="http://openapi")

    assert seen == {"method": "DELETE", "path": "/v1/jobs/abc123"}
    assert result == {"job_id": "abc123", "status": "deleted"}


def test_external_blast_delete_job_transport_error_is_503(monkeypatch) -> None:
    from api.services import external_blast

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def delete(self, _path: str) -> object:
            raise httpx.ConnectError("sibling unreachable")

    monkeypatch.setattr(external_blast.httpx, "Client", FakeClient)

    with pytest.raises(HTTPException) as raised:
        external_blast.delete_job("abc123", base_url="http://openapi")

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "openapi_unreachable"


def test_openapi_runtime_round_trip() -> None:
    from api.services.openapi import runtime as openapi_runtime

    class FakeRedis:
        def __init__(self) -> None:
            self.value: str | None = None

        def set(self, _key: str, value: str) -> None:
            self.value = value

        def get(self, _key: str) -> str | None:
            return self.value

    fake = FakeRedis()
    assert openapi_runtime.save_openapi_base_url(
        "http://10.0.0.4/",
        metadata={"cluster_name": "elb-cluster"},
        client=fake,
    )
    assert openapi_runtime.get_openapi_base_url(client=fake) == "http://10.0.0.4"


def test_external_blast_file_download_forwards(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "stream_file",
        lambda job_id, file_id: external_blast.StreamedFile(
            chunks=iter([b"<Blast", b"Output />"]),
            media_type="application/xml",
            filename="blast_result.xml",
        ),
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/files/result-xml-001")

    assert response.status_code == 200
    assert response.content == b"<BlastOutput />"
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["content-disposition"] == 'attachment; filename="blast_result.xml"'


def test_canonical_jobs_list_merges_external_when_table_unconfigured(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "list_jobs",
        lambda: {
            "jobs": [
                {
                    "job_id": "aaaaaaaaaaaa",
                    "status": "running",
                    "created_at": "2026-05-12T10:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                    "cluster_name": "elb-cluster",
                }
            ],
            "count": 1,
        },
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"][0]["job_id"] == "aaaaaaaaaaaa"
    assert body["jobs"][0]["source"] == "external_api"
    assert body["jobs"][0]["infrastructure"] == {"cluster_name": "elb-cluster"}
    assert "degraded" not in body


def test_canonical_jobs_list_uses_cluster_openapi_context(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast
    from api.services.blast import external_jobs

    captured: dict[str, str] = {}

    monkeypatch.setattr(
        external_jobs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}.{resource_group}.{subscription_id}",
            "api_token": "cluster-token",
        },
    )

    def list_jobs(**kwargs):
        captured.update(kwargs)
        return {
            "jobs": [
                {
                    "job_id": "bbbbbbbbbbbb",
                    "status": "success",
                    "created_at": "2026-05-12T10:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                }
            ],
            "count": 1,
        }

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    client = TestClient(app)

    response = client.get(
        "/api/blast/jobs?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    assert captured == {
        "base_url": "http://elb-cluster.rg-elb-01.sub-1",
        "api_token": "cluster-token",
    }
    body = response.json()
    assert body["jobs"][0]["job_id"] == "bbbbbbbbbbbb"
    assert body["jobs"][0]["status"] == "completed"


def test_discover_subscription_clusters_skips_stopped(monkeypatch):
    """Stopped clusters are excluded so the polled Recent searches endpoint
    never pays a 10 s ``k8s_get_service_ip`` timeout per Stopped cluster.

    A Stopped cluster's OpenAPI plane is down (no running pods), so it can
    never serve a live ``/v1/jobs`` row anyway; anything it ran while Running
    was already synced into the Table.
    """
    import api.services as services_pkg
    import api.services.monitoring as monitoring_pkg
    from api.services.blast import external_jobs

    external_jobs._reset_external_jobs_cache()
    monkeypatch.setattr(
        monitoring_pkg,
        "list_aks_clusters_in_subscription",
        lambda _cred, _sub: [
            {"name": "running-a", "resource_group": "rg-1", "power_state": "Running"},
            {"name": "stopped-b", "resource_group": "rg-2", "power_state": "Stopped"},
            {"name": "unknown-c", "resource_group": "rg-3", "power_state": None},
        ],
    )
    monkeypatch.setattr(services_pkg, "get_credential", lambda: object())

    pairs = external_jobs._discover_subscription_clusters("sub-1")

    # Running + unknown power state are kept; explicitly Stopped is dropped.
    assert ("rg-1", "running-a") in pairs
    assert ("rg-3", "unknown-c") in pairs
    assert ("rg-2", "stopped-b") not in pairs


def test_collect_and_sync_external_jobs_discovers_and_upserts(monkeypatch):
    """The shared orchestration resolves the subscription's clusters, fetches
    each one's ``/v1/jobs`` list, and upserts the discovered rows into the
    Table. One unreachable cluster is recorded but does not abort the others.
    """
    from api.services import external_blast
    from api.services.blast import external_jobs
    from fastapi import HTTPException

    external_jobs._reset_external_jobs_cache()

    monkeypatch.setattr(
        external_jobs,
        "_discover_subscription_clusters",
        lambda subscription_id: [("rg-1", "up-cluster"), ("rg-2", "down-cluster")],
    )
    monkeypatch.setattr(
        external_jobs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}",
            "api_token": "tok",
        },
    )

    def list_jobs(**kwargs):
        if kwargs.get("base_url") == "http://down-cluster":
            raise HTTPException(503, detail={"code": "openapi_unreachable"})
        return {
            "jobs": [
                {
                    "job_id": "ext-aaa",
                    "status": "running",
                    "created_at": "2026-06-13T00:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                    "cluster_name": "up-cluster",
                }
            ],
            "count": 1,
        }

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)

    synced: list[list[dict]] = []

    def _fake_sync(rows, *, caller_oid, tenant_id=""):
        synced.append(rows)
        return (len(rows), 0, set())

    monkeypatch.setattr(external_jobs, "_sync_external_jobs_to_table", _fake_sync)

    result = external_jobs.collect_and_sync_external_jobs(
        subscription_id="sub-1", tenant_id="tid-1"
    )

    # The up cluster's row was discovered, scope-defaulted, and synced.
    assert [r["job_id"] for r in result.rows] == ["ext-aaa"]
    assert result.rows[0]["subscription_id"] == "sub-1"
    assert result.rows[0]["resource_group"] == "rg-1"
    assert result.created == 1
    assert synced and synced[0][0]["job_id"] == "ext-aaa"
    # Partial failure: one target answered, the down one is recorded not raised.
    assert result.any_target_ok is True
    assert len(result.target_failures) == 1


def test_collect_and_sync_external_jobs_never_raises(monkeypatch):
    """Target resolution blowing up degrades to an empty result, never raises."""
    from api.services.blast import external_jobs

    external_jobs._reset_external_jobs_cache()

    def _boom(*_a, **_kw):
        raise RuntimeError("resolution exploded")

    monkeypatch.setattr(external_jobs, "_resolve_external_list_targets", _boom)

    result = external_jobs.collect_and_sync_external_jobs(subscription_id="sub-1")
    assert result.rows == []
    assert result.created == 0
    assert len(result.target_failures) == 1


def test_canonical_jobs_list_subscription_scope_discovers_clusters(monkeypatch):
    """Subscription-only listing (Recent searches) discovers every cluster's
    OpenAPI endpoint so jobs submitted directly through ``POST /v1/jobs`` show
    up even though no ``cluster_name`` is pinned.

    Reproduces the production bug: the Recent searches history view omits
    ``cluster_name`` to list across all clusters, but
    ``_openapi_client_kwargs_from_cluster`` needs the full triple, so the
    external listing resolved to the env/runtime fallback and directly-submitted
    jobs were invisible. The route now enumerates the subscription's clusters.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast
    from api.services.blast import external_jobs

    monkeypatch.setattr(
        external_jobs,
        "_discover_subscription_clusters",
        lambda subscription_id: [
            ("rg-elb-01", "elb-cluster-a"),
            ("rg-elb-02", "elb-cluster-b"),
        ],
    )
    monkeypatch.setattr(
        external_jobs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: (
            {"base_url": f"http://{cluster_name}", "api_token": "tok"}
            if cluster_name
            else {}
        ),
    )
    # Active rows (the "running" job below) trigger a per-job detail refresh
    # via `external_blast.get_job`. Without this stub it makes a real HTTP GET
    # to the fake `http://elb-cluster-b` base URL and burns the full client
    # timeout (~10 s) before degrading — the detail miss is swallowed, so the
    # assertions still pass, but the test would needlessly tarpit CI. Return a
    # no-op detail so the merge keeps the original row.
    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id, **_kwargs: {"job_id": job_id, "status": "running"},
    )

    jobs_by_base = {
        "http://elb-cluster-a": {
            "jobs": [
                {
                    "job_id": "aaaaaaaaaaaa",
                    "status": "success",
                    "created_at": "2026-05-12T10:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                }
            ],
            "count": 1,
        },
        "http://elb-cluster-b": {
            "jobs": [
                {
                    "job_id": "bbbbbbbbbbbb",
                    "status": "running",
                    "created_at": "2026-05-12T11:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                }
            ],
            "count": 1,
        },
    }

    def list_jobs(**kwargs):
        return jobs_by_base.get(kwargs.get("base_url", ""), {"jobs": [], "count": 0})

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    client = TestClient(app)

    response = client.get("/api/blast/jobs?subscription_id=sub-1")

    assert response.status_code == 200
    body = response.json()
    job_ids = {job["job_id"] for job in body["jobs"]}
    assert job_ids == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}
    assert "external_degraded" not in body


def test_canonical_jobs_list_subscription_scope_partial_cluster_failure(monkeypatch):
    """A single Stopped/unreachable cluster must not hide jobs on the other
    reachable clusters, and must not flag the whole list as degraded."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast
    from api.services.blast import external_jobs
    from fastapi import HTTPException

    monkeypatch.setattr(
        external_jobs,
        "_discover_subscription_clusters",
        lambda subscription_id: [
            ("rg-elb-01", "up-cluster"),
            ("rg-elb-02", "down-cluster"),
        ],
    )
    monkeypatch.setattr(
        external_jobs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}",
            "api_token": "tok",
        },
    )

    def list_jobs(**kwargs):
        if kwargs.get("base_url") == "http://down-cluster":
            raise HTTPException(
                503, detail={"code": "openapi_unreachable", "message": "down"}
            )
        return {
            "jobs": [
                {
                    "job_id": "cccccccccccc",
                    "status": "success",
                    "created_at": "2026-05-12T10:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                }
            ],
            "count": 1,
        }

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    client = TestClient(app)

    response = client.get("/api/blast/jobs?subscription_id=sub-1")

    assert response.status_code == 200
    body = response.json()
    assert [job["job_id"] for job in body["jobs"]] == ["cccccccccccc"]
    # Partial success (one cluster answered) is not flagged as degraded.
    assert "external_degraded" not in body


def test_canonical_jobs_list_subscription_scope_all_clusters_down(monkeypatch):
    """When every discovered cluster is unreachable the list degrades with an
    ``external_degraded`` flag so the SPA can warn that OpenAPI-submitted jobs
    may be missing."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast
    from api.services.blast import external_jobs
    from fastapi import HTTPException

    monkeypatch.setattr(
        external_jobs,
        "_discover_subscription_clusters",
        lambda subscription_id: [("rg-elb-01", "down-cluster")],
    )
    monkeypatch.setattr(
        external_jobs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}",
            "api_token": "tok",
        },
    )

    def list_jobs(**_kwargs):
        raise HTTPException(
            503, detail={"code": "openapi_unreachable", "message": "down"}
        )

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    client = TestClient(app)

    response = client.get("/api/blast/jobs?subscription_id=sub-1")

    assert response.status_code == 200
    body = response.json()
    assert body.get("external_degraded") is True
    assert body["external_degraded_reason"] == "openapi_unreachable"


@pytest.mark.slow
def test_canonical_jobs_list_filters_local_rows_by_scope(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo

    class ScopedRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return [
                SimpleNamespace(
                    job_id="matching-job",
                    task_id=None,
                    type="blast",
                    status="completed",
                    phase="completed",
                    created_at="2026-05-19T10:00:00Z",
                    updated_at="2026-05-19T10:01:00Z",
                    error_code=None,
                    parent_job_id=None,
                    payload={
                        "subscription_id": "sub-1",
                        "resource_group": "rg-elb-01",
                        "cluster_name": "elb-cluster",
                        "program": "blastn",
                        "db": "core_nt",
                    },
                ),
                SimpleNamespace(
                    job_id="other-cluster-job",
                    task_id=None,
                    type="blast",
                    status="completed",
                    phase="completed",
                    created_at="2026-05-19T11:00:00Z",
                    updated_at="2026-05-19T11:01:00Z",
                    error_code=None,
                    parent_job_id=None,
                    payload={
                        "subscription_id": "sub-1",
                        "resource_group": "rg-elb-01",
                        "cluster_name": "other-cluster",
                        "program": "blastn",
                        "db": "core_nt",
                    },
                ),
            ]

        def list_children_for_owner(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(state_repo, "JobStateRepository", ScopedRepo)
    monkeypatch.setattr(external_blast, "list_jobs", lambda **_kwargs: {"jobs": []})
    client = TestClient(app)

    response = client.get(
        "/api/blast/jobs?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    assert [job["job_id"] for job in response.json()["jobs"]] == ["matching-job"]


def test_canonical_jobs_list_enriches_external_rows_with_detail(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo
    from api.services.blast import external_jobs

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    captured: dict[str, dict[str, str]] = {}

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(
        external_jobs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}.{resource_group}.{subscription_id}",
            "api_token": "cluster-token",
        },
    )

    def list_jobs(**kwargs):
        captured["list"] = kwargs
        return {
            "jobs": [
                {
                    "job_id": "cccccccccccc",
                    "status": "running",
                    "created_at": "2026-05-19T10:42:09Z",
                    "program": "blastn",
                    "db": "https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt",
                    "cluster_name": "elb-cluster",
                }
            ],
            "count": 1,
        }

    def get_job(job_id: str, **kwargs):
        captured["get"] = {"job_id": job_id, **kwargs}
        return {
            "job_id": job_id,
            "status": "success",
            "created_at": "2026-05-19T10:42:09Z",
            "completed_at": "2026-05-19T10:44:14Z",
            "program": "blastn",
            "db_name": "16S_ribosomal_RNA",
            "query_file": "queries/uploads/probe/query.fa",
            "cluster_name": "elb-cluster",
            "execution": {
                "shard_count": 1,
                "shards_succeeded": 1,
                "shards_active": 0,
                "shards_failed": 0,
            },
            "result": {"files": [{"name": "batch_000.out.gz"}]},
        }

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    monkeypatch.setattr(external_blast, "get_job", get_job)
    client = TestClient(app)

    response = client.get(
        "/api/blast/jobs?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    assert captured["list"] == {
        "base_url": "http://elb-cluster.rg-elb-01.sub-1",
        "api_token": "cluster-token",
    }
    assert captured["get"] == {
        "job_id": "cccccccccccc",
        "base_url": "http://elb-cluster.rg-elb-01.sub-1",
        "api_token": "cluster-token",
    }
    row = response.json()["jobs"][0]
    assert row["status"] == "completed"
    assert row["phase"] == "completed"
    assert row["db"] == "16S_ribosomal_RNA"
    assert row["query_label"] == "query.fa"
    assert row["splits_total"] == 1
    assert row["splits_done"] == 1
    assert row["output"]["execution"]["shards_succeeded"] == 1


def test_canonical_jobs_list_shows_remembered_inline_query_label(monkeypatch):
    """An inline-FASTA API submit remembers the first defline so Recent
    searches shows the real query instead of the generic ``query.fa``.

    The sibling OpenAPI plane returns no query identity on the job record, so
    without the submit-time bridge the projection falls back to ``query.fa``.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo
    from api.services.blast import external_jobs
    from api.services.blast import external_query_labels as eql

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

        def list_for_scope(self, *_args, **_kwargs):
            return []

    # In-memory OPS Redis so remember/recall round-trips without a server.
    fake_store: dict[str, str] = {}

    class _FakeRedis:
        def set(self, key, value, ex=None):
            fake_store[key] = value

        def get(self, key):
            value = fake_store.get(key)
            return value.encode("utf-8") if value is not None else None

    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda **_kw: _FakeRedis()
    )
    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(
        external_jobs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}.{resource_group}.{subscription_id}",
            "api_token": "cluster-token",
        },
    )
    monkeypatch.setattr(
        external_blast,
        "submit_job",
        lambda payload: {"job_id": "dddddddddddd", "status": "queued"},
    )
    monkeypatch.setattr(
        external_blast,
        "list_jobs",
        lambda **_kwargs: {
            "jobs": [
                {
                    "job_id": "dddddddddddd",
                    "status": "running",
                    "created_at": "2026-06-10T00:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                    "cluster_name": "elb-cluster",
                }
            ],
            "count": 1,
        },
    )
    # Detail enrichment is not exercised here (the list row carries enough).
    monkeypatch.setattr(
        external_blast, "get_job", lambda job_id, **_kw: {"job_id": job_id, "status": "running"}
    )

    client = TestClient(app)

    submit = client.post(
        "/api/blast/jobs",
        json={"query_fasta": ">NC_003310.1 cowpox\nATGGAGAAGCGAGAAGTTAA", "db": "core_nt"},
    )
    assert submit.status_code == 202
    # The bridge remembered the defline for the upstream job id.
    assert eql.recall_query_label("dddddddddddd") == "NC_003310.1"

    listing = client.get(
        "/api/blast/jobs?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )
    assert listing.status_code == 200
    rows = [row for row in listing.json()["jobs"] if row["job_id"] == "dddddddddddd"]
    assert rows, "external job missing from listing"
    assert rows[0]["query_label"] == "NC_003310.1"


@pytest.mark.parametrize("external_status", ["success", "completed"])
def test_external_completed_status_maps_to_dashboard_completed(external_status):
    from api.routes import _blast_shared as stubs

    assert stubs._external_status_to_dashboard(external_status) == "completed"


def test_sync_external_recovers_servicebus_submission_source(monkeypatch):
    """A queue-drained job keeps its ``servicebus`` origin label.

    Over the wire the sibling only knows ``external_api`` (its enum has no
    ``servicebus`` value), so a /v1/jobs row always reports ``external_api``.
    The dashboard's own stored row preserves the true marker under
    ``payload.external.submission_source``; the sync must recover it so Recent
    searches / Jobs label the row ``servicebus`` instead of downgrading it.
    """
    from api.routes import _blast_shared as shared
    from api.services import state_repo

    class FakeExisting:
        job_id = "sb-1"
        status = "running"
        phase = "running"
        error_code = ""
        subscription_id = "sub-1"
        resource_group = "rg-1"
        cluster_name = "elb-cluster"
        storage_account = ""
        program = "blastn"
        db = "core_nt"
        job_title = "blastn core_nt"
        query_label = "q.fa"
        payload: ClassVar[dict] = {"external": {"submission_source": "servicebus"}}

    class FakeRepo:
        def get_many(self, _ids):
            return {"sb-1": FakeExisting()}

        def create(self, state):
            raise AssertionError("existing row must update, not create")

        def update(self, *_a, **_kw):
            return None

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(state_repo, "JobState", object)

    ext = {
        "job_id": "sb-1",
        "status": "running",
        "submission_source": "external_api",  # sibling wire value
        "external_correlation_id": "corr-1",
        "program": "blastn",
        "db": "core_nt",
        "cluster_name": "elb-cluster",
    }
    shared._sync_external_jobs_to_table([ext], caller_oid="oid-1")
    # The dashboard's own servicebus marker wins over the sibling's external_api,
    # mutating the same dict the list route projects (shared by reference).
    assert ext["submission_source"] == "servicebus"


def test_sync_external_does_not_invent_servicebus_for_plain_external(monkeypatch):
    """A genuine external_api job (no stored servicebus marker) is never
    relabelled — the recovery only honours a marker the dashboard itself set."""
    from api.routes import _blast_shared as shared
    from api.services import state_repo

    class FakeExisting:
        job_id = "ext-1"
        status = "running"
        phase = "running"
        error_code = ""
        subscription_id = "sub-1"
        resource_group = "rg-1"
        cluster_name = "elb-cluster"
        storage_account = ""
        program = "blastn"
        db = "core_nt"
        job_title = "blastn core_nt"
        query_label = "q.fa"
        payload: ClassVar[dict] = {"external": {"submission_source": "external_api"}}

    class FakeRepo:
        def get_many(self, _ids):
            return {"ext-1": FakeExisting()}

        def create(self, state):
            raise AssertionError("existing row must update, not create")

        def update(self, *_a, **_kw):
            return None

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(state_repo, "JobState", object)

    ext = {
        "job_id": "ext-1",
        "status": "running",
        "submission_source": "external_api",
        "program": "blastn",
        "db": "core_nt",
        "cluster_name": "elb-cluster",
    }
    shared._sync_external_jobs_to_table([ext], caller_oid="oid-1")
    assert ext["submission_source"] == "external_api"


def test_sync_external_jobs_creates_missing_rows(monkeypatch):
    """External rows that the Table does not yet know about MUST be
    persisted on first sight so they survive an AKS restart."""
    from api.routes import _blast_shared as shared

    created: list[object] = []
    updated: list[dict[str, object]] = []

    class FakeRepo:
        def get_many(self, ids):
            return {}

        def create(self, state):
            created.append(state)
            return state

        def update(self, *_a, **_kw):
            updated.append({"args": _a, "kw": _kw})

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.job_id = kwargs.get("job_id")
            self.status = kwargs.get("status")
            self.phase = kwargs.get("phase")

    monkeypatch.setattr(
        shared,
        "_sync_external_jobs_to_table",
        shared._sync_external_jobs_to_table,
    )

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "running",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "cluster_name": "elb-cluster",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (1, 0, set())
    assert len(created) == 1
    assert created[0].kwargs["job_id"] == "abc123"
    assert created[0].kwargs["status"] == "running"
    assert updated == []


def test_sync_external_failed_new_row_recovers_error_into_error_code(monkeypatch):
    """A new external row first seen already FAILED must recover the real
    cause from the sibling /jobs/{id} detail (the LIST snapshot has no
    ``error``) and persist it into the error_code column."""
    from api.routes import _blast_shared as shared
    from api.services import external_blast, state_repo

    created: list[object] = []
    calls: list[str] = []

    class FakeRepo:
        def get_many(self, ids):
            return {}

        def create(self, state):
            created.append(state)
            return state

        def update(self, *_a, **_kw):
            raise AssertionError("new row should be created, not updated")

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.job_id = kwargs.get("job_id")
            self.status = kwargs.get("status")
            self.phase = kwargs.get("phase")

    def fake_get_job(job_id, **_kwargs):
        calls.append(job_id)
        return {
            "job_id": job_id,
            "status": "failed",
            "error": {
                "code": "BLAST_FAILED",
                "message": "memory requirements exceed memory available on machine type",
            },
        }

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)
    monkeypatch.setattr(external_blast, "get_job", fake_get_job)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "fail-new-1",
                "status": "failed",
                "created_at": "2026-06-14T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "cluster_name": "elb-cluster",
                "subscription_id": "sub-1",
                "resource_group": "rg-1",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result[0] == 1  # created
    assert calls == ["fail-new-1"]
    assert "memory requirements exceed" in (created[0].kwargs.get("error_code") or "")


def test_sync_external_failed_transition_recovers_error(monkeypatch):
    """An existing row transitioning to FAILED with an empty error_code must
    fetch the sibling detail once and persist its message."""
    from types import SimpleNamespace

    from api.routes import _blast_shared as shared
    from api.services import external_blast, state_repo

    updated: list[tuple[str, dict[str, object]]] = []
    calls: list[str] = []

    existing = SimpleNamespace(
        status="running",
        phase="running",
        error_code="",
        subscription_id="",
        resource_group="",
        cluster_name="",
        storage_account="",
        program="blastn",
        db="core_nt",
        job_title="core_nt",
        query_label="q",
    )

    class FakeRepo:
        def get_many(self, ids):
            return {"fail-tx-1": existing}

        def create(self, state):
            raise AssertionError("existing row should update, not create")

        def update(self, job_id, **kw):
            updated.append((job_id, kw))

    def fake_get_job(job_id, **_kwargs):
        calls.append(job_id)
        return {
            "job_id": job_id,
            "status": "failed",
            "error": {"code": "BLAST_FAILED", "message": "selected machine type too small"},
        }

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(external_blast, "get_job", fake_get_job)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "fail-tx-1",
                "status": "failed",
                "created_at": "2026-06-14T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "cluster_name": "elb-cluster",
            }
        ],
        caller_oid="x",
    )

    assert result[1] == 1  # updated
    assert calls == ["fail-tx-1"]
    assert any(
        "selected machine type too small" in str(kw.get("error_code") or "")
        for _jid, kw in updated
    )


def test_sync_external_failed_transition_skips_recovery_when_error_code_present(monkeypatch):
    """The recovery fetch is once-only: a failed row that already carries an
    error_code must NOT re-hit the sibling on every poll."""
    from types import SimpleNamespace

    from api.routes import _blast_shared as shared
    from api.services import external_blast, state_repo

    calls: list[str] = []

    existing = SimpleNamespace(
        status="running",
        phase="running",
        error_code="prior_real_error",
        subscription_id="",
        resource_group="",
        cluster_name="",
        storage_account="",
        program="blastn",
        db="core_nt",
        job_title="core_nt",
        query_label="q",
    )

    class FakeRepo:
        def get_many(self, ids):
            return {"fail-guard-1": existing}

        def create(self, state):
            raise AssertionError("existing row should update, not create")

        def update(self, job_id, **kw):
            pass

    def fake_get_job(job_id, **_kwargs):
        calls.append(job_id)
        return {"job_id": job_id, "status": "failed", "error": {"message": "should not be read"}}

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(external_blast, "get_job", fake_get_job)

    shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "fail-guard-1",
                "status": "failed",
                "created_at": "2026-06-14T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "cluster_name": "elb-cluster",
            }
        ],
        caller_oid="x",
    )

    assert calls == []  # no recovery fetch when error_code already present


def test_sync_external_failed_recovery_never_breaks_sync_on_sibling_outage(monkeypatch):
    """If the sibling detail fetch raises, the sync must still succeed and the
    row is created/updated without an error_code (generic banner preserved)."""
    from api.routes import _blast_shared as shared
    from api.services import external_blast, state_repo

    created: list[object] = []

    class FakeRepo:
        def get_many(self, ids):
            return {}

        def create(self, state):
            created.append(state)
            return state

        def update(self, *_a, **_kw):
            pass

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.job_id = kwargs.get("job_id")
            self.status = kwargs.get("status")
            self.phase = kwargs.get("phase")

    def boom_get_job(job_id, **_kwargs):
        raise RuntimeError("sibling unreachable")

    monkeypatch.setattr(state_repo, "JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)
    monkeypatch.setattr(external_blast, "get_job", boom_get_job)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "fail-outage-1",
                "status": "failed",
                "created_at": "2026-06-14T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "cluster_name": "elb-cluster",
            }
        ],
        caller_oid="x",
    )

    assert result[0] == 1  # created despite the sibling outage
    assert created[0].kwargs.get("error_code") is None



def test_sync_external_jobs_persists_remembered_query_label(monkeypatch):
    """The sync MUST durably persist the submit-time defline label into the
    Table row even when the caller did not pre-apply it to the row.

    This makes the label survive a revision restart that drops OPS Redis:
    once the first list call materialises the row, the label lives in the
    durable Table independent of Redis. The sync is responsible for the
    injection itself (does not rely on the route having applied it first).
    """
    from api.routes import _blast_shared as shared

    created: list[object] = []

    class FakeRepo:
        def get_many(self, ids):
            return {}

        def create(self, state):
            created.append(state)
            return state

        def update(self, *_a, **_kw):
            pass

    class FakeRepoFactory:
        def __call__(self):
            return FakeRepo()

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.job_id = kwargs.get("job_id")
            self.status = kwargs.get("status")
            self.phase = kwargs.get("phase")

    from api.services import state_repo
    from api.services.blast import external_query_labels as eql

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    # Remembered label keyed by the upstream job id; the input row carries NO
    # query identity (mirrors a raw sibling list row).
    monkeypatch.setattr(eql, "recall_query_label", lambda job_id: "NC_003310.1")

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "running",
                "created_at": "2026-06-10T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "cluster_name": "elb-cluster",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (1, 0, set())
    assert len(created) == 1
    assert created[0].kwargs["query_label"] == "NC_003310.1"


def test_sync_external_jobs_updates_drifted_status(monkeypatch):
    """When the external plane reports a different status than the Table,
    the Table row MUST be refreshed so the list view is not stale."""
    from api.routes import _blast_shared as shared

    updated_calls: list[dict[str, object]] = []
    created: list[object] = []

    class ExistingRow:
        status = "running"
        phase = "running"
        # Already carries good identity metadata, so the heal backfill is a
        # no-op here and this test stays focused on status-drift behaviour.
        job_id = "abc123"
        job_title = "blastn - core_nt"
        program = "blastn"
        db = "core_nt"
        query_label = "query.fa"

    class FakeRepo:
        def get_many(self, ids):
            return {"abc123": ExistingRow()}

        def update(self, job_id, **kwargs):
            updated_calls.append({"job_id": job_id, **kwargs})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "success",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 1, set())
    assert created == []
    assert updated_calls == [
        {"job_id": "abc123", "status": "completed", "phase": "completed"}
    ]


def test_sync_external_jobs_heals_degenerate_identity_columns(monkeypatch):
    """A row first synced from a transient /v1/jobs row that lacked program/db
    was persisted with the canonical defaults (program/job_title="blast",
    db=""). The list view reads those columns directly (include_payload=False),
    so the API job stayed stuck showing "blast" with no database. Once the
    sibling list carries the real program/db, the sync MUST heal the identity
    columns durably."""
    from api.routes import _blast_shared as shared

    updated_calls: list[dict[str, object]] = []
    created: list[object] = []

    class DegenerateRow:
        job_id = "abc123"
        status = "running"
        phase = "running"
        job_title = "blast"
        program = "blast"
        db = ""
        query_label = ""

    class FakeRepo:
        def get_many(self, ids):
            return {"abc123": DegenerateRow()}

        def update(self, job_id, **kwargs):
            updated_calls.append({"job_id": job_id, **kwargs})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "running",
                "created_at": "2026-06-14T02:55:00Z",
                "program": "blastn",
                "db": "https://stg.blob.core.windows.net/blast-db/core_nt/core_nt",
                "cluster_name": "elb-cluster-01",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 1, set())
    assert created == []
    assert len(updated_calls) == 1
    call = updated_calls[0]
    assert call["job_id"] == "abc123"
    # Identity columns healed from the fresh projection (db -> basename).
    assert call["program"] == "blastn"
    assert call["db"] == "core_nt"
    assert call["job_title"] == "blastn - core_nt"
    # Status/phase unchanged (already "running") so they are NOT in the patch.
    assert "status" not in call
    assert "phase" not in call


def test_sync_external_jobs_does_not_overwrite_good_identity_columns(monkeypatch):
    """A row that already carries real program/db/title MUST NOT be clobbered
    by the heal backfill — even if the fresh upstream values differ in casing
    or path. The heal only fills the degenerate canonical default."""
    from api.routes import _blast_shared as shared

    updated_calls: list[dict[str, object]] = []

    class GoodRow:
        job_id = "abc123"
        status = "running"
        phase = "running"
        job_title = "My custom search"
        program = "blastn"
        db = "core_nt"
        query_label = "NC_003310.1"

    class FakeRepo:
        def get_many(self, ids):
            return {"abc123": GoodRow()}

        def update(self, job_id, **kwargs):
            updated_calls.append({"job_id": job_id, **kwargs})

        def create(self, state):  # pragma: no cover - defensive
            raise AssertionError("must not create an existing row")

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "running",
                "created_at": "2026-06-14T02:55:00Z",
                "program": "tblastn",
                "db": "swissprot",
                "job_title": "Upstream title",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    # No status drift and no degenerate columns -> nothing to write at all.
    assert result == (0, 0, set())
    assert updated_calls == []


def test_sync_external_jobs_clears_stale_error_code_on_terminal_flip(monkeypatch):
    """When the sibling now reports success but the Table row still carries a
    stale ``error_code`` (e.g. a transient ``worker_lost`` left by a
    false-positive reconcile pass), the sync MUST clear it durably so the
    dashboard row stops showing the recovered error indefinitely (issue #2)."""
    from api.routes import _blast_shared as shared

    updated_calls: list[dict[str, object]] = []
    created: list[object] = []

    class ExistingRow:
        job_id = "abc123"
        status = "running"
        phase = "worker_lost"
        error_code = "worker_lost"
        job_title = "blastn - core_nt"
        program = "blastn"
        db = "core_nt"
        query_label = "query.fa"

    class FakeRepo:
        def get_many(self, ids):
            return {"abc123": ExistingRow()}

        def update(self, job_id, **kwargs):
            updated_calls.append({"job_id": job_id, **kwargs})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "completed",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 1, set())
    assert updated_calls and updated_calls[0]["job_id"] == "abc123"
    assert updated_calls[0]["status"] == "completed"
    assert updated_calls[0]["phase"] == "completed"
    # The stale worker_lost MUST be cleared by an explicit "" write so the
    # repository's patch-semantics update actually unsets the column.
    assert updated_calls[0]["error_code"] == ""


def test_sync_external_jobs_skips_unchanged_status(monkeypatch):
    """If the status has not drifted, the sync MUST NOT call update.

    This avoids appending a new jobhistory row on every poll cycle."""
    from api.routes import _blast_shared as shared

    updated_calls: list[object] = []
    created: list[object] = []

    class ExistingRow:
        job_id = "abc123"
        status = "completed"
        phase = "completed"
        job_title = "blastn - core_nt"
        program = "blastn"
        db = "core_nt"
        query_label = "query.fa"

    class FakeRepo:
        def get_many(self, ids):
            return {"abc123": ExistingRow()}

        def update(self, *_a, **_kw):
            updated_calls.append({"args": _a, "kw": _kw})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "success",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 0, set())


def test_sync_external_jobs_backfills_empty_cluster_scope(monkeypatch):
    """A /v1/jobs row first stored with no cluster_name MUST be backfilled
    once a later poll resolves the cluster, so the AKS cluster card (which
    filters by cluster_name) shows it under the same rule as Recent searches.

    The status is unchanged here, proving the backfill happens on its own —
    not only as a side effect of a status drift update."""
    from api.routes import _blast_shared as shared

    updated_calls: list[dict[str, object]] = []
    created: list[object] = []

    class ExistingRow:
        job_id = "bf-job-1"
        status = "running"
        phase = "running"
        subscription_id = ""
        resource_group = ""
        cluster_name = ""
        storage_account = ""
        job_title = "blastn - core_nt"
        program = "blastn"
        db = "core_nt"
        query_label = "query.fa"

    class FakeRepo:
        def get_many(self, ids):
            return {"bf-job-1": ExistingRow()}

        def update(self, job_id, **kwargs):
            updated_calls.append({"job_id": job_id, **kwargs})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "bf-job-1",
                "status": "running",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                # Scope injected by a cluster-scoped poll's scope defaults.
                "subscription_id": "sub-1",
                "resource_group": "rg-elb-cluster",
                "cluster_name": "elb-cluster-01",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 1, set())
    assert created == []
    # Status did not drift, so only the empty scope columns are written.
    assert updated_calls == [
        {
            "job_id": "bf-job-1",
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": "elb-cluster-01",
        }
    ]


def test_sync_external_jobs_does_not_overwrite_existing_cluster_scope(monkeypatch):
    """The backfill MUST only fill empty columns — a row that already carries a
    cluster_name must never be clobbered by a poll resolving a different scope,
    and an unchanged status with a fully-populated row triggers no update."""
    from api.routes import _blast_shared as shared

    updated_calls: list[dict[str, object]] = []

    class ExistingRow:
        job_id = "bf-job-2"
        status = "running"
        phase = "running"
        subscription_id = "sub-1"
        resource_group = "rg-elb-cluster"
        cluster_name = "elb-cluster-01"
        storage_account = "stexisting"
        job_title = "blastn - core_nt"
        program = "blastn"
        db = "core_nt"
        query_label = "query.fa"

    class FakeRepo:
        def get_many(self, ids):
            return {"bf-job-2": ExistingRow()}

        def update(self, job_id, **kwargs):
            updated_calls.append({"job_id": job_id, **kwargs})

        def create(self, state):  # pragma: no cover - not expected
            raise AssertionError("create must not be called for an existing row")

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "bf-job-2",
                "status": "running",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "subscription_id": "sub-1",
                "resource_group": "rg-elb-cluster",
                "cluster_name": "elb-cluster-99",
                "storage_account": "stother",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 0, set())
    assert updated_calls == []


def test_external_jobs_cache_serves_repeat_requests(monkeypatch):
    """Two back-to-back calls within the TTL MUST hit the upstream only once."""
    from api.routes import _blast_shared as shared

    hits = {"count": 0}

    class FakeExternal:
        @staticmethod
        def list_jobs(**_kwargs):
            hits["count"] += 1
            return {"jobs": [{"job_id": "x1"}], "count": 1}

    from api.services import external_blast

    monkeypatch.setattr(external_blast, "list_jobs", FakeExternal.list_jobs)
    # Cache fixture in conftest already cleared it; do nothing extra.

    rows1 = shared._external_list_jobs_cached({"base_url": "http://cluster"})
    rows2 = shared._external_list_jobs_cached({"base_url": "http://cluster"})

    assert rows1 == rows2 == [{"job_id": "x1"}]
    assert hits["count"] == 1


def test_external_jobs_cache_caches_http_failures(monkeypatch):
    """``HTTPException`` from the upstream MUST be cached for a short TTL so
    SPA polling doesn't keep paying the 700-1500 ms round-trip to learn the
    same 401 again. Subsequent calls within the TTL re-raise the cached
    exception without invoking the upstream.
    """
    from api.routes import _blast_shared as shared
    from api.services import external_blast

    hits = {"count": 0}

    def fail_with_401(**_kwargs):
        hits["count"] += 1
        raise HTTPException(
            401,
            detail={"code": "openapi_http_401", "detail": "missing token"},
        )

    monkeypatch.setattr(external_blast, "list_jobs", fail_with_401)

    with pytest.raises(HTTPException) as first:
        shared._external_list_jobs_cached({"base_url": "http://cluster"})
    with pytest.raises(HTTPException) as second:
        shared._external_list_jobs_cached({"base_url": "http://cluster"})

    assert first.value.status_code == 401
    assert second.value.status_code == 401
    assert hits["count"] == 1


def test_canonical_jobs_list_reports_external_detail_code(monkeypatch):
    """Runtime failures from the external plane (5xx, network, timeout) MUST
    still surface as ``external_degraded`` so operators can see real outages
    in the request inspector. Configuration-absence reasons
    (``openapi_not_configured``) are exercised by
    :func:`test_canonical_jobs_list_silent_when_external_not_configured`.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    def list_jobs_unavailable():
        raise HTTPException(
            502,
            detail={"code": "openapi_upstream_error", "message": "bad gateway"},
        )

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(external_blast, "list_jobs", list_jobs_unavailable)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] == []
    assert body["external_degraded"] is True
    assert body["external_degraded_reason"] == "openapi_upstream_error"
    # The SPA Recent searches page renders this so the external-plane outage is
    # not swallowed silently. Prefer the upstream client's structured message.
    assert body["external_degraded_message"] == "bad gateway"
    assert "degraded" not in body


def test_canonical_jobs_list_silent_when_external_not_configured(monkeypatch):
    """``openapi_not_configured`` / ``openapi_not_enabled`` mean the optional
    external execution plane simply isn't wired up — that's a normal state,
    not a degradation. The Jobs payload MUST NOT carry ``external_degraded``
    in that case, otherwise the request inspector renders every 30 s poll as
    a red Degraded badge and operators tune out the real warnings.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    def list_jobs_not_configured():
        raise HTTPException(
            503,
            detail={
                "code": "openapi_not_configured",
                "message": "ELB_OPENAPI_BASE_URL is not set",
            },
        )

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(external_blast, "list_jobs", list_jobs_not_configured)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] == []
    assert "external_degraded" not in body
    assert "external_degraded_reason" not in body
    assert "degraded" not in body


@pytest.mark.parametrize(
    "reason_code",
    ["openapi_not_configured", "openapi_not_enabled"],
)
def test_canonical_jobs_list_silent_for_every_not_enabled_reason(monkeypatch, reason_code):
    """Lock the entire ``_EXTERNAL_NOT_ENABLED_REASONS`` allow-list. Adding a
    new code to that set without an accompanying test would let a regression
    slip through.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import _blast_shared as stubs_module
    from api.services import external_blast, state_repo

    assert reason_code in stubs_module._EXTERNAL_NOT_ENABLED_REASONS

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    def list_jobs_not_enabled():
        raise HTTPException(
            503,
            detail={"code": reason_code, "message": f"{reason_code} active"},
        )

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(external_blast, "list_jobs", list_jobs_not_enabled)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert "external_degraded" not in body
    assert "external_degraded_reason" not in body
    assert "degraded" not in body


def test_canonical_submit_delegates_dashboard_payload(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import blast as stubs

    def fake_submit(body, caller):
        assert body == {"program": "blastn"}
        assert caller.object_id
        return {
            "id": "local-job-1",
            "job_id": "local-job-1",
            "instance_id": "task-1",
            "status": "queued",
        }

    monkeypatch.setattr(stubs, "blast_submit", fake_submit)
    client = TestClient(app)

    response = client.post("/api/blast/jobs", json={"program": "blastn"})

    assert response.status_code == 202
    assert response.json()["job_id"] == "local-job-1"
    assert response.json()["instance_id"] == "task-1"


def test_canonical_job_get_falls_back_to_external(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "success",
            "created_at": "2026-05-12T10:00:00Z",
            "completed_at": "2026-05-12T10:03:00Z",
            "db_name": "core_nt",
            "result": {"files": []},
        },
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs/aaaaaaaaaaaa")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["phase"] == "completed"
    assert body["db"] == "core_nt"


def test_canonical_results_list_falls_back_to_external_files(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "success",
            "result": {
                "files": [
                    {
                        "file_id": "result-001",
                        "filename": "batch_001.xml",
                        "format": "blast_xml",
                        "size_bytes": 123,
                    }
                ]
            },
        },
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs/aaaaaaaaaaaa/results")

    assert response.status_code == 200
    body = response.json()
    assert body["files"] == body["results"]
    assert body["files"][0]["file_id"] == "result-001"
    assert body["files"][0]["name"] == "batch_001.xml"


def test_canonical_result_file_streams_external_file_id(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "stream_file",
        lambda job_id, file_id: external_blast.StreamedFile(
            chunks=iter([b"<Blast", b"Output />"]),
            media_type="application/xml",
            filename="batch_001.xml",
        ),
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs/aaaaaaaaaaaa/results/result-001")

    assert response.status_code == 200
    assert response.content == b"<BlastOutput />"
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["content-disposition"] == 'attachment; filename="batch_001.xml"'


def test_canonical_result_fallback_threads_scope_query(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    captured: dict[str, Any] = {}

    def fake_get_job(job_id: str, **kwargs: Any) -> dict[str, Any]:
        captured["get_job"] = {"job_id": job_id, **kwargs}
        return {
            "job_id": job_id,
            "status": "success",
            "result": {"files": [{"file_id": "result-001", "filename": "batch_001.xml"}]},
        }

    def fake_stream_file(job_id: str, file_id: str, **kwargs: Any) -> external_blast.StreamedFile:
        captured["stream_file"] = {"job_id": job_id, "file_id": file_id, **kwargs}
        return external_blast.StreamedFile(
            chunks=iter([b"<Blast", b"Output />"]),
            media_type="application/xml",
            filename="batch_001.xml",
        )

    monkeypatch.setattr(external_blast, "get_job", fake_get_job)
    monkeypatch.setattr(external_blast, "stream_file", fake_stream_file)
    client = TestClient(app)
    params = {
        "subscription_id": "sub-a",
        "resource_group": "rg-a",
        "cluster_name": "aks-a",
    }

    list_response = client.get("/api/blast/jobs/aaaaaaaaaaaa/results", params=params)
    file_response = client.get("/api/blast/jobs/aaaaaaaaaaaa/results/result-001", params=params)

    assert list_response.status_code == 200
    assert file_response.status_code == 200
    assert captured["get_job"]["subscription_id"] == "sub-a"
    assert captured["get_job"]["resource_group"] == "rg-a"
    assert captured["get_job"]["cluster_name"] == "aks-a"
    assert captured["stream_file"]["subscription_id"] == "sub-a"
    assert captured["stream_file"]["resource_group"] == "rg-a"
    assert captured["stream_file"]["cluster_name"] == "aks-a"


def test_canonical_local_result_file_id_must_match_job_prefix(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services.storage.data import encode_blob_file_id

    client = TestClient(app)
    other_job_file_id = encode_blob_file_id("bbbbbbbbbbbb/batch_001.xml")

    response = client.get(
        f"/api/blast/jobs/aaaaaaaaaaaa/results/{other_job_file_id}",
        params={"storage_account": "stexample"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file_id"


def test_local_blob_file_id_round_trips_and_rejects_traversal() -> None:
    from api.services.storage.data import decode_blob_file_id, encode_blob_file_id

    file_id = encode_blob_file_id("aaaaaaaaaaaa/batch_001.xml")

    assert decode_blob_file_id(file_id) == "aaaaaaaaaaaa/batch_001.xml"
    with pytest.raises(ValueError):
        decode_blob_file_id(encode_blob_file_id("../secret.xml"))


def test_submit_marks_row_broker_unavailable_when_celery_down(monkeypatch):
    """If the broker is unreachable, the row MUST flip to failed/broker_unavailable
    instead of sitting on ``queued`` forever — otherwise the dashboard counts
    a zombie row as an active job."""
    from fastapi import HTTPException as _HTTPException

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import blast as stubs

    created: list[object] = []
    updates: list[tuple[str, dict[str, object]]] = []

    class FakeRepo:
        def create(self, state):
            created.append(state)
            return state

        def update(self, job_id, **kwargs):
            updates.append((job_id, kwargs))

    class FakeRepoFactory:
        def __call__(self):
            return FakeRepo()

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())

    def broker_down(*_args, **_kwargs):
        raise _HTTPException(
            status_code=503,
            detail={"code": "broker_unavailable", "retryable": True},
        )

    monkeypatch.setattr(stubs, "_safe_delay", broker_down)

    client = TestClient(app)
    response = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "blast-db/core_nt/core_nt",
            "query_file": "queries/q.fa",
        },
    )

    assert response.status_code == 503
    assert len(created) == 1
    assert updates, "broker failure must trigger row cleanup"
    assert updates[0][1]["status"] == "failed"
    assert updates[0][1]["phase"] == "broker_unavailable"
    assert updates[0][1]["error_code"] == "broker_unavailable"


def test_sync_skips_tombstoned_deleted_rows(monkeypatch):
    """A row tombstoned by ``DELETE /api/blast/jobs/{id}`` (status=='deleted')
    MUST NOT be resurrected by a subsequent external-OpenAPI sync."""
    from api.routes import _blast_shared as shared

    class TombstoneRow:
        status = "deleted"
        phase = "deleted"

    updates: list[object] = []
    created: list[object] = []

    class FakeRepo:
        def get_many(self, _ids):
            return {"zzz999": TombstoneRow()}

        def update(self, *_a, **_kw):
            updates.append({"a": _a, "kw": _kw})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "zzz999",
                "status": "completed",  # upstream still thinks the job exists
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
            }
        ],
        caller_oid="",
    )

    assert result == (0, 0, {"zzz999"})


@pytest.mark.slow
def test_canonical_jobs_list_refreshes_active_local_rows(monkeypatch):
    """List endpoint must refresh active rows so the dashboard doesn't wait
    up to 60 s for the next beat reconcile to flip a finished job."""

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes.blast import jobs as jobs_route
    from api.services import external_blast, state_repo

    active_row = SimpleNamespace(
        job_id="active-row",
        task_id=None,
        type="blast",
        status="running",
        phase="running",
        created_at="2026-05-21T00:00:00Z",
        updated_at="2026-05-21T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        subscription_id="sub-1",
        resource_group="rg-elb-01",
        cluster_name="elb-cluster",
        storage_account="stelb",
        payload={},
    )
    terminal_row = SimpleNamespace(
        job_id="terminal-row",
        task_id=None,
        type="blast",
        status="completed",
        phase="completed",
        created_at="2026-05-21T00:00:00Z",
        updated_at="2026-05-21T00:01:30Z",
        error_code=None,
        parent_job_id=None,
        subscription_id="sub-1",
        resource_group="rg-elb-01",
        cluster_name="elb-cluster",
        storage_account="stelb",
        payload={},
    )

    class ListRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return [active_row, terminal_row]

        def list_children_for_owner(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(state_repo, "JobStateRepository", ListRepo)
    monkeypatch.setattr(external_blast, "list_jobs", lambda **_kwargs: {"jobs": []})

    refreshed_ids: list[str] = []

    def fake_refresh(repo, state):
        refreshed_ids.append(state.job_id)
        return state

    monkeypatch.setattr(jobs_route, "_refresh_running_blast_state", fake_refresh)

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs"
        "?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    # Only the active row's phase ∈ _K8S_REFRESH_PHASES → refresh called once.
    assert refreshed_ids == ["active-row"]


# ── /v1/ready integration ──────────────────────────────────────────────────


class _FakeReadyClient:
    """httpx.Client double for ``external_blast.ready`` tests.

    ``status`` and ``payload`` describe the sibling response; ``exc`` instead
    raises a transport error on ``.get()`` to exercise the openapi_unreachable
    branch.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        payload: dict[str, object] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._status = status
        self._payload = payload or {}
        self._exc = exc
        self.captured: dict[str, object] = {}

    def __call__(self, **kwargs: object) -> _FakeReadyClient:
        self.captured.update(kwargs)
        return self

    def __enter__(self) -> _FakeReadyClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, path: str) -> httpx.Response:
        self.captured["path"] = path
        if self._exc is not None:
            raise self._exc
        return httpx.Response(
            status_code=self._status,
            json=self._payload,
            request=httpx.Request("GET", f"http://openapi{path}"),
        )


def test_external_blast_ready_returns_payload_on_200(monkeypatch) -> None:
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _FakeReadyClient(
        status=200,
        payload={
            "ready": True,
            "checks": {
                "k8s_api": {"status": "ok"},
                "workload_pool": {"status": "ok", "ready_nodes": 3},
                "openapi_pod": {"status": "ok", "ready_replicas": 1},
            },
            "version": "3.7.0",
        },
    )
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    result = external_blast.ready(base_url="http://openapi", api_token="t")

    assert result["ready"] is True
    assert result["version"] == "3.7.0"
    assert fake.captured["timeout"] == external_blast._READY_TIMEOUT_SECONDS
    assert fake.captured["path"] == "/v1/ready"


def test_external_blast_ready_503_surfaces_upstream_code(monkeypatch) -> None:
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _FakeReadyClient(
        status=503,
        payload={
            "ready": False,
            "code": "no_workload_nodes",
            "message": "No Ready nodes match label 'workload=blast'",
            "checks": {
                "k8s_api": {"status": "ok"},
                "workload_pool": {
                    "status": "error",
                    "ready_nodes": 0,
                    "label": "workload=blast",
                },
            },
        },
    )
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    with pytest.raises(HTTPException) as raised:
        external_blast.ready(base_url="http://openapi")

    assert raised.value.status_code == 503
    detail = raised.value.detail
    assert detail["code"] == "openapi_not_ready"
    assert detail["upstream_code"] == "no_workload_nodes"
    assert "workload_pool" in detail["checks"]


def test_external_blast_ready_transport_error_is_openapi_unreachable(monkeypatch) -> None:
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _FakeReadyClient(exc=httpx.ConnectTimeout("AKS API server unreachable"))
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    with pytest.raises(HTTPException) as raised:
        external_blast.ready(base_url="http://openapi")

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "openapi_unreachable"
    assert raised.value.detail["probe"] == "ready"


class _SequencedReadyClient:
    """httpx.Client double that returns a different status on each call.

    Used to exercise the reactive 401 → token-resync → retry path: the first
    probe answers 401, the retried probe (after resync) answers 200.
    """

    def __init__(self, statuses: list[int], payloads: list[dict[str, object]]) -> None:
        self._statuses = statuses
        self._payloads = payloads
        self.calls = 0
        self.tokens_seen: list[object] = []

    def __call__(self, **kwargs: object) -> _SequencedReadyClient:
        headers = kwargs.get("headers") or {}
        if isinstance(headers, dict):
            self.tokens_seen.append(headers.get("X-ELB-API-Token"))
        return self

    def __enter__(self) -> _SequencedReadyClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, path: str) -> httpx.Response:
        idx = min(self.calls, len(self._statuses) - 1)
        status = self._statuses[idx]
        payload = self._payloads[idx]
        self.calls += 1
        return httpx.Response(
            status_code=status,
            json=payload,
            request=httpx.Request("GET", f"http://openapi{path}"),
        )


def test_external_blast_ready_401_resyncs_token_and_retries(monkeypatch) -> None:
    """A 401 from /v1/ready means the dashboard's cached token went stale
    (ephemeral Redis wiped by a redeploy). The gate must re-read the live
    token from the cluster, sync it, and retry once — turning the 401 into
    a normal ready payload without operator action."""
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _SequencedReadyClient(
        statuses=[401, 200],
        payloads=[
            {"detail": "Unauthorized"},
            {"ready": True, "version": "3.7.2"},
        ],
    )
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    resync_calls = {"n": 0}

    def _fake_resync() -> str:
        resync_calls["n"] += 1
        return "healed-token-xyz"

    monkeypatch.setattr(
        "api.services.openapi.token.resync_openapi_api_token_from_cluster",
        _fake_resync,
    )

    result = external_blast.ready(base_url="http://openapi", api_token="stale-token")

    assert result["ready"] is True
    assert result["version"] == "3.7.2"
    assert resync_calls["n"] == 1
    # The retry probe carried the freshly-resynced token, not the stale one.
    assert fake.tokens_seen[-1] == "healed-token-xyz"
    assert fake.calls == 2


def test_external_blast_ready_401_without_recovery_surfaces_error(monkeypatch) -> None:
    """If the resync cannot recover a token (no cluster context, RBAC, etc.)
    the original 401 must surface — and must NOT retry endlessly."""
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _SequencedReadyClient(
        statuses=[401, 401],
        payloads=[{"detail": "Unauthorized"}, {"detail": "Unauthorized"}],
    )
    monkeypatch.setattr(external_blast.httpx, "Client", fake)
    monkeypatch.setattr(
        "api.services.openapi.token.resync_openapi_api_token_from_cluster",
        lambda: "",
    )

    with pytest.raises(HTTPException) as raised:
        external_blast.ready(base_url="http://openapi", api_token="stale-token")

    assert raised.value.status_code == 401
    # Resync returned "" → no retry probe fired (single upstream call).
    assert fake.calls == 1


def test_external_blast_ready_404_fails_open(monkeypatch, caplog) -> None:
    """Older sibling images lack /v1/ready — the gate must not block submit."""
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _FakeReadyClient(status=404, payload={"detail": "Not Found"})
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    with caplog.at_level("WARNING", logger=external_blast.LOGGER.name):
        result = external_blast.ready(base_url="http://openapi")

    assert result["ready"] is True
    assert result["skipped"] == "version_mismatch"
    # Operators must see a structured warning so a pre-4.15 sibling cannot
    # silently degrade the gate.
    assert any(
        getattr(rec, "event", None) == "ready_probe_stale_sibling"
        for rec in caplog.records
    )


def test_external_blast_ready_caches_success_within_ttl(monkeypatch) -> None:
    """A second call within TTL must not hit the sibling at all."""
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _FakeReadyClient(
        status=200,
        payload={"ready": True, "version": "3.7.1"},
    )
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    first = external_blast.ready(base_url="http://openapi", api_token="t")
    # Mutate the fake to a fatal response. If the cache works, second call
    # returns the cached success and never sees this.
    fake._status = 503
    fake._payload = {"code": "k8s_unreachable"}
    second = external_blast.ready(base_url="http://openapi", api_token="t")

    assert first == second
    assert first["version"] == "3.7.1"


def test_external_blast_ready_429_surfaces_rate_limit(monkeypatch) -> None:
    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _FakeReadyClient(
        status=429,
        payload={
            "ready": False,
            "code": "rate_limited",
            "message": "/v1/ready rate limit reached (30/min). Retry after 60s.",
            "limit_per_minute": 30,
        },
    )
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    with pytest.raises(HTTPException) as raised:
        external_blast.ready(base_url="http://openapi", api_token="t")

    assert raised.value.status_code == 429
    assert raised.value.detail["code"] == "openapi_ready_rate_limited"
    assert raised.value.detail["limit_per_minute"] == 30


def test_external_blast_ready_cache_key_normalises_base_url(monkeypatch) -> None:
    """Trailing slash and case differences must hit the same cache slot."""
    from api.services import external_blast

    external_blast.reset_ready_cache()
    a = external_blast._ready_cache_key("https://x.io", "tok")
    b = external_blast._ready_cache_key("https://x.io/", "tok")
    c = external_blast._ready_cache_key("HTTPS://X.IO/", "tok")
    assert a == b == c
    # Token still hashed to a full sha256 hex (64 chars), not the old [:8].
    assert len(a[1]) == 64


def test_external_blast_ready_inflight_serialises_concurrent_callers(
    monkeypatch,
) -> None:
    """N concurrent cache-miss callers must produce exactly one upstream call."""
    import threading

    from api.services import external_blast

    external_blast.reset_ready_cache()

    call_count = {"n": 0}
    call_lock = threading.Lock()
    block = threading.Event()
    leader_in = threading.Event()

    payload = {"ready": True, "version": "3.7.1"}

    class _SlowClient:
        def __call__(self, **_kwargs: object) -> _SlowClient:
            return self

        def __enter__(self) -> _SlowClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _path: str) -> httpx.Response:
            with call_lock:
                call_count["n"] += 1
            leader_in.set()
            # Hold the leader inside the upstream call so waiters pile up.
            block.wait(timeout=2.0)
            return httpx.Response(
                status_code=200,
                json=payload,
                request=httpx.Request("GET", "http://openapi/v1/ready"),
            )

    monkeypatch.setattr(external_blast.httpx, "Client", _SlowClient())

    results: list[dict[str, object]] = []
    results_lock = threading.Lock()

    def _probe() -> None:
        out = external_blast.ready(base_url="http://openapi", api_token="t")
        with results_lock:
            results.append(out)

    threads = [threading.Thread(target=_probe) for _ in range(8)]
    for th in threads:
        th.start()
    # Make sure the leader is inside .get() before we release it.
    assert leader_in.wait(timeout=1.0)
    block.set()
    for th in threads:
        th.join(timeout=3.0)
        assert not th.is_alive()

    assert call_count["n"] == 1, f"expected single upstream call, saw {call_count['n']}"
    assert len(results) == 8
    assert all(r == payload for r in results)


def test_external_blast_ready_cache_hit_logs_event(monkeypatch, caplog) -> None:
    """Subsequent cache hits must still emit a ``ready_probe_cached`` log line.

    Otherwise App Insights silently undercounts outage duration during the TTL
    window. Failure cache hits log at INFO; success hits at DEBUG.
    """
    import logging as _logging

    from api.services import external_blast

    external_blast.reset_ready_cache()
    fake = _FakeReadyClient(
        status=503,
        payload={"ready": False, "code": "k8s_unreachable", "message": "down"},
    )
    monkeypatch.setattr(external_blast.httpx, "Client", fake)

    caplog.set_level(_logging.INFO, logger="api.services.external_blast")

    # First call: real upstream (cached as HTTPException).
    with pytest.raises(HTTPException):
        external_blast.ready(base_url="http://openapi", api_token="t")
    caplog.clear()

    # Second call: cache hit. Must log ``ready_probe_cached`` at INFO with
    # the failure code preserved.
    with pytest.raises(HTTPException):
        external_blast.ready(base_url="http://openapi", api_token="t")

    cached_logs = [
        rec for rec in caplog.records if getattr(rec, "event", None) == "ready_probe_cached"
    ]
    assert cached_logs, "expected a ready_probe_cached log on cache hit"
    rec = cached_logs[0]
    assert getattr(rec, "status", None) == 503
    assert getattr(rec, "code", None) == "openapi_not_ready"
    assert getattr(rec, "cached_age_seconds", None) is not None


def test_external_blast_submit_aborts_when_ready_blocks(monkeypatch) -> None:
    """Submit must surface the sibling's 503 verbatim and never call submit_job."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    submit_called = {"count": 0}

    def fake_ready(**_kwargs: object) -> dict[str, object]:
        raise HTTPException(
            503,
            detail={
                "code": "openapi_not_ready",
                "upstream_code": "openapi_pod_not_ready",
                "message": "elb-openapi Deployment has zero ready replicas",
                "checks": {"openapi_pod": {"status": "error", "ready_replicas": 0}},
            },
        )

    def fake_submit(payload: dict[str, object]) -> dict[str, object]:
        # pragma: no cover - asserted not called
        submit_called["count"] += 1
        return {"job_id": "ignored"}

    monkeypatch.setattr(external_blast, "ready", fake_ready)
    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
        },
    )

    assert response.status_code == 503
    detail = response.json()
    assert detail["code"] == "openapi_not_ready"
    assert detail["upstream_code"] == "openapi_pod_not_ready"
    assert submit_called["count"] == 0


def test_external_blast_submit_proceeds_when_ready_ok(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    call_order: list[str] = []

    def fake_ready(**_kwargs: object) -> dict[str, object]:
        call_order.append("ready")
        return {"ready": True, "version": "3.7.0"}

    def fake_submit(payload: dict[str, object]) -> dict[str, object]:
        call_order.append("submit")
        return {
            "job_id": "abcdef123456",
            "status": "queued",
            "created_at": "2026-05-29T10:00:00Z",
        }

    monkeypatch.setattr(external_blast, "ready", fake_ready)
    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
        },
    )

    assert response.status_code == 202
    assert call_order == ["ready", "submit"]
    assert response.json()["job_id"] == "abcdef123456"


def test_canonical_jobs_list_marks_running_row_stale_when_cluster_stopped(monkeypatch):
    """End-to-end: a ``running`` local row whose AKS cluster is stopped must
    come back with ``stale=True`` + ``refresh_blocked_reason`` so the SPA can
    render a "status frozen — cluster stopped" badge instead of a misleading
    live progress bar. The expensive K8s refresh is skipped for that row."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes.blast import jobs as jobs_route
    from api.services import cluster_health, external_blast, state_repo

    jobs_route._reset_blast_jobs_list_cache()

    class RunningRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return [
                SimpleNamespace(
                    job_id="running-job",
                    task_id=None,
                    type="blast",
                    status="running",
                    phase="running",
                    created_at="2026-06-01T10:00:00Z",
                    updated_at="2026-06-01T10:01:00Z",
                    error_code=None,
                    parent_job_id=None,
                    payload={
                        "subscription_id": "sub-1",
                        "resource_group": "rg-elb-01",
                        "cluster_name": "elb-cluster",
                        "program": "blastn",
                        "db": "core_nt",
                    },
                ),
            ]

        def list_children_for_owner(self, *_args, **_kwargs):
            return {}

    refresh_calls: list[str] = []

    def _should_not_refresh(_repo, row):
        refresh_calls.append(str(row.job_id))
        return row

    monkeypatch.setattr(state_repo, "JobStateRepository", RunningRepo)
    monkeypatch.setattr(external_blast, "list_jobs", lambda **_kwargs: {"jobs": []})
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        cluster_health,
        "get_cluster_health",
        lambda *_args, **_kwargs: {
            "healthy": False,
            "exists": True,
            "power_state": "Stopped",
            "reason": "cluster_stopped",
        },
    )
    monkeypatch.setattr(jobs_route, "_refresh_running_blast_state", _should_not_refresh)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_id"] == "running-job"
    assert job["stale"] is True
    assert job["refresh_blocked_reason"] == "cluster_stopped"
    assert job["cluster_power_state"] == "Stopped"
    # The K8s refresh is skipped for the blocked row (no ~10 s timeout).
    assert refresh_calls == []


def test_external_error_message_rejects_long_body_as_code():
    """An elastic-blast failure arrives as a free-form string (or a dict whose
    `code` is the whole multi-line error body incl. a REDACTED x-ms-* header
    dump). `error_code` must stay a short token; the long text becomes the
    (length-capped) message instead."""
    from api.services.blast.external_jobs import _external_error_message

    long_body = (
        "'x-ms-owner': 'REDACTED'\n'x-ms-acl': 'REDACTED'\n"
        "2026-06-04T22:16:10Z ERROR: BLAST database "
        "https://acct.blob.core.windows.net/blast-db/core_nt/core_nt memory "
        "requirements exceed memory available on selected machine type "
        '"Standard_E16s_v5". ' * 6
    )

    # (1) Plain string error → no code, message clamped + whitespace-collapsed.
    code, message = _external_error_message(long_body)
    assert code is None
    assert message is not None
    assert "\n" not in message
    assert len(message) <= 2000

    # (2) Dict whose "code" is actually the long body → code rejected, body
    # preserved as the message.
    code2, message2 = _external_error_message({"code": long_body})
    assert code2 is None
    assert message2 is not None
    assert len(message2) <= 2000

    # (3) A real short code is preserved.
    code3, message3 = _external_error_message(
        {"code": "database_not_found", "message": "BLAST DB missing"}
    )
    assert code3 == "database_not_found"
    assert message3 == "BLAST DB missing"

    # (4) Empty / falsy error → both None.
    assert _external_error_message(None) == (None, None)
    assert _external_error_message("") == (None, None)


def _capture_submit_transport(monkeypatch, responder):
    """Install a MockTransport for httpx.Client and capture posted JSON bodies.

    ``responder`` receives the 0-based attempt index and the parsed JSON body
    and must return an ``httpx.Response`` (or raise an ``httpx`` transport
    error to simulate a connection failure). Returns the list that collects
    each attempt's posted body.
    """
    import json as _json

    from api.services import external_blast

    bodies: list[dict] = []
    state = {"attempt": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        idx = state["attempt"]
        state["attempt"] += 1
        return responder(idx, body)

    transport = httpx.MockTransport(handler)
    original_client_cls = httpx.Client

    class _StubClient(original_client_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(external_blast.httpx, "Client", _StubClient)
    return bodies


def test_submit_job_derives_idempotency_key_from_correlation_id(monkeypatch) -> None:
    """No caller idempotency_key → derive one from external_correlation_id.

    Without this the sibling (which dedupes ONLY on idempotency_key) cannot
    collapse a retried submit, so a lost-response retry would duplicate the
    cluster job.
    """
    from api.services import external_blast

    bodies = _capture_submit_transport(
        monkeypatch,
        lambda idx, body: httpx.Response(202, json={"job_id": "j1", "status": "queued"}),
    )

    result = external_blast.submit_job(
        {"external_correlation_id": "corr-123", "program": "blastn", "db": "core_nt"},
        base_url="http://openapi",
    )

    assert result["job_id"] == "j1"
    assert len(bodies) == 1
    assert bodies[0]["idempotency_key"] == "corr-123"
    # The correlation id is preserved alongside the derived key.
    assert bodies[0]["external_correlation_id"] == "corr-123"


def test_submit_job_does_not_mutate_caller_payload(monkeypatch) -> None:
    from api.services import external_blast

    _capture_submit_transport(
        monkeypatch,
        lambda idx, body: httpx.Response(202, json={"job_id": "j1", "status": "queued"}),
    )

    payload = {"external_correlation_id": "corr-xyz", "program": "blastn", "db": "core_nt"}
    external_blast.submit_job(payload, base_url="http://openapi")

    # The caller's dict must not gain an idempotency_key (we copy internally).
    assert "idempotency_key" not in payload


def test_submit_job_preserves_caller_idempotency_key(monkeypatch) -> None:
    from api.services import external_blast

    bodies = _capture_submit_transport(
        monkeypatch,
        lambda idx, body: httpx.Response(202, json={"job_id": "j1", "status": "queued"}),
    )

    external_blast.submit_job(
        {
            "external_correlation_id": "corr-123",
            "idempotency_key": "caller-key",
            "program": "blastn",
            "db": "core_nt",
        },
        base_url="http://openapi",
    )

    # Caller-supplied idempotency_key always wins over the derived correlation id.
    assert bodies[0]["idempotency_key"] == "caller-key"


def test_submit_job_retry_resends_same_idempotency_key(monkeypatch) -> None:
    """A retried submit must re-send the SAME idempotency_key so the sibling
    dedupes it to one cluster job instead of creating a duplicate."""
    from api.services import external_blast

    # No backoff sleeps in the test.
    monkeypatch.setattr(external_blast, "_SUBMIT_MAX_TRANSPORT_RETRIES", 2)
    monkeypatch.setattr(external_blast, "_SUBMIT_RETRY_BACKOFF_SECONDS", (0.0, 0.0))

    def responder(idx: int, body: dict) -> httpx.Response:
        if idx == 0:
            raise httpx.ConnectError("sibling unreachable")
        return httpx.Response(202, json={"job_id": "j1", "status": "queued"})

    bodies = _capture_submit_transport(monkeypatch, responder)

    result = external_blast.submit_job(
        {"external_correlation_id": "corr-retry", "program": "blastn", "db": "core_nt"},
        base_url="http://openapi",
    )

    assert result["job_id"] == "j1"
    assert len(bodies) == 2  # first failed, second succeeded
    assert bodies[0]["idempotency_key"] == bodies[1]["idempotency_key"] == "corr-retry"


def test_submit_job_without_any_key_does_not_retry(monkeypatch) -> None:
    """No idempotency_key AND no external_correlation_id → the sibling cannot
    dedupe, so a transport failure must surface immediately (no retry) to avoid
    duplicate jobs."""
    from api.services import external_blast

    monkeypatch.setattr(external_blast, "_SUBMIT_MAX_TRANSPORT_RETRIES", 2)
    monkeypatch.setattr(external_blast, "_SUBMIT_RETRY_BACKOFF_SECONDS", (0.0, 0.0))

    attempts = {"n": 0}

    def responder(idx: int, body: dict) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("sibling unreachable")

    _capture_submit_transport(monkeypatch, responder)

    with pytest.raises(HTTPException) as raised:
        external_blast.submit_job(
            {"program": "blastn", "db": "core_nt"},
            base_url="http://openapi",
        )

    assert raised.value.status_code == 503
    assert attempts["n"] == 1  # surfaced on the first failure, no retry
