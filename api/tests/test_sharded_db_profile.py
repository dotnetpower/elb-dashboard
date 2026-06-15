"""Tests for the server-derived sharded-DB resource_profile default.

Responsibility: Verify ``resolve_sharded_db_resource_profile`` promotes a
    missing/standard profile to a sharding DB's default (core_nt → core_nt_safe),
    preserves explicit profiles, and that both submit paths (direct OpenAPI +
    Service Bus drain) apply it.
Edit boundaries: Pure helper + payload-shaping behaviour; no live sibling calls.
Key entry points: ``resolve_sharded_db_resource_profile``.
Risky contracts: core_nt MUST run sharded (memory-fit); an explicit sharding
    profile or a non-core_nt DB is never altered.
Validation: ``uv run pytest -q api/tests/test_sharded_db_profile.py``.
"""

from __future__ import annotations

import pytest
from api.services.blast.submit_payload import resolve_sharded_db_resource_profile


@pytest.mark.parametrize(
    ("database", "requested", "expected"),
    [
        # core_nt with no / standard profile → promoted to the sharding default.
        ("core_nt", None, "core_nt_safe"),
        ("core_nt", "", "core_nt_safe"),
        ("core_nt", "standard", "core_nt_safe"),
        # Full blob-URL form of core_nt is recognised via extract_db_name.
        (
            "https://acct.blob.core.windows.net/blast-db/core_nt/core_nt",
            "standard",
            "core_nt_safe",
        ),
        ("blast-db/core_nt/core_nt", "standard", "core_nt_safe"),
        # Explicit sharding-family profile is preserved as-is.
        ("core_nt", "core_nt_precise", "core_nt_precise"),
        ("core_nt", "precise", "precise"),
        ("core_nt", "core_nt_safe", "core_nt_safe"),
        # Small / unknown DBs keep standard (no sharding).
        ("16S_ribosomal_RNA", "standard", "standard"),
        ("16S_ribosomal_RNA", None, "standard"),
        ("nt_something_unknown", "", "standard"),
    ],
)
def test_resolve_profile(database: str, requested: object, expected: str) -> None:
    assert resolve_sharded_db_resource_profile(database, requested) == expected


def test_non_standard_custom_profile_preserved() -> None:
    # A caller that set a non-standard, non-sharding profile keeps it (we only
    # upgrade empty/standard).
    assert resolve_sharded_db_resource_profile("core_nt", "custom_x") == "custom_x"


def test_sharding_defaults_are_recognised_sharding_profiles() -> None:
    """Contract guard: every promoted default MUST itself be a profile the
    sibling treats as 'shard this DB'. Otherwise the promotion would hand the
    sibling a profile it does not act on and the DB would silently run
    unsharded again (the exact bug this feature fixes). Keep this green when the
    sibling's sharding-profile set or the per-DB default map changes."""
    from api.services.blast.submit_payload import (
        _SHARDED_DB_DEFAULT_PROFILE,
        _SHARDING_RESOURCE_PROFILES,
    )

    assert _SHARDED_DB_DEFAULT_PROFILE, "expected at least one sharded-DB default"
    for db_name, profile in _SHARDED_DB_DEFAULT_PROFILE.items():
        assert profile in _SHARDING_RESOURCE_PROFILES, (
            f"default profile {profile!r} for {db_name!r} is not in the sibling's "
            f"sharding-profile set {_SHARDING_RESOURCE_PROFILES} — the sibling would "
            "not shard it"
        )


def test_direct_submit_promotes_core_nt(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/v1/elastic-blast/submit forwards core_nt_safe to the sibling
    even when the caller sends no resource_profile."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("ENABLE_SB_SUBMIT_INGRESS", "false")
    from api.main import app
    from api.services import external_blast
    from fastapi.testclient import TestClient

    captured: dict[str, object] = {}

    def fake_submit(payload):
        captured.update(payload)
        return {"job_id": "abc123def456", "status": "queued"}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    client = TestClient(app)

    resp = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q\nACGTACGT", "db": "core_nt", "program": "blastn"},
    )
    assert resp.status_code == 202, resp.text
    assert captured["resource_profile"] == "core_nt_safe"


def test_direct_submit_preserves_explicit_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("ENABLE_SB_SUBMIT_INGRESS", "false")
    from api.main import app
    from api.services import external_blast
    from fastapi.testclient import TestClient

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        external_blast, "submit_job", lambda payload: captured.update(payload) or {"job_id": "x"}
    )
    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    client = TestClient(app)

    resp = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q\nACGTACGT",
            "db": "core_nt",
            "program": "blastn",
            "resource_profile": "core_nt_precise",
        },
    )
    assert resp.status_code == 202, resp.text
    assert captured["resource_profile"] == "core_nt_precise"


def test_servicebus_drain_promotes_core_nt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Service Bus drain → OpenAPI bridge promotes core_nt to core_nt_safe."""
    from api.services.service_bus import ParsedMessage
    from api.services.service_bus_pref import ServiceBusConfig
    from api.tasks.servicebus import tasks as sb

    msg = ParsedMessage(
        body={
            "query_fasta": ">q\nACGTACGT",
            "db": "core_nt",
            "program": "blastn",
            "external_correlation_id": "corr-1",
        },
        raw_body="",
        message_id="m1",
        correlation_id="corr-1",
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=1,
    )
    payload = sb._build_request_payload(msg, ServiceBusConfig())
    assert payload is not None
    assert payload["resource_profile"] == "core_nt_safe"
    assert payload["submission_source"] == "servicebus"
