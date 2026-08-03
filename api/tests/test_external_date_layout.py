"""Date-tiered results layout for EXTERNAL (SB / OpenAPI) submits.

When ``STORAGE_DATE_LAYOUT_ENABLED`` is on, every external submit surface flows
through ``external_blast.submit_job`` which forwards a ``results_prefix`` of the
shape ``YYYY/MM/DD/`` to the sibling so it writes results under
``results/<YYYY/MM/DD>/<job_id>/`` — matching the native date tiering. With the
flag off (default), no prefix is sent and the sibling keeps the flat layout.

Validation: ``uv run pytest -q api/tests/test_external_date_layout.py``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from api.services import external_blast


class _Resp:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"job_id": "abc123"}


def _mock_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_e: Any) -> None:
            return None

        def post(self, *_a: Any, **kwargs: Any) -> _Resp:
            captured["json"] = kwargs.get("json")
            return _Resp()

    monkeypatch.setattr(external_blast.httpx, "Client", _Client)
    # Avoid per-cluster runtime cache lookups / token resync touching real state.
    monkeypatch.setattr(external_blast, "_base_url", lambda *a, **k: "http://sib")
    monkeypatch.setattr(
        external_blast,
        "_request_with_token_resync",
        lambda *, send, **_k: send(_Client()),
    )
    return captured


def test_submit_job_injects_date_prefix_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    captured = _mock_capture(monkeypatch)

    external_blast.submit_job({"db": "core_nt", "external_correlation_id": "corr-1"})

    sent = captured["json"]
    assert re.fullmatch(r"\d{4}/\d{2}/\d{2}/", sent["results_prefix"]), sent.get("results_prefix")


def test_submit_job_no_prefix_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DATE_LAYOUT_ENABLED", raising=False)
    captured = _mock_capture(monkeypatch)

    external_blast.submit_job({"db": "core_nt", "external_correlation_id": "corr-2"})

    assert "results_prefix" not in captured["json"]


def test_submit_job_caller_prefix_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    captured = _mock_capture(monkeypatch)

    external_blast.submit_job(
        {"db": "core_nt", "external_correlation_id": "corr-3", "results_prefix": "1999/01/01/"}
    )

    assert captured["json"]["results_prefix"] == "1999/01/01/"


def test_submit_job_respects_explicit_empty_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit "" is a caller choosing the flat layout; injection must NOT
    # overwrite it with the date prefix (only an ABSENT key triggers injection).
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    captured = _mock_capture(monkeypatch)

    external_blast.submit_job(
        {"db": "core_nt", "external_correlation_id": "corr-4", "results_prefix": ""}
    )

    assert captured["json"]["results_prefix"] == ""


def test_read_chain_resolves_stored_dated_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 4 regression guard: the analytics read path must resolve an
    external job's stored date-tiered prefix, not the flat ``{job_id}/``.

    The drain persists ``results_prefix='YYYY/MM/DD/<job_id>/'`` on the JobState
    row; with the flag ON, ``resolve_results_prefix`` reads it back via the
    Table lookup and ``list_parseable_result_blobs`` lists the date path. Before
    the fix it listed ``<job_id>/`` and found zero blobs (no_results) even
    though the sibling wrote everything under the date directory.
    """
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    job_id = "c75483f2a08c"
    dated = f"2026/06/23/{job_id}/"

    class _Row:
        results_prefix = dated

    class _Repo:
        def get(self, _job_id: str) -> Any:
            return _Row()

    from api.services import state_repo
    from api.services.blast import result_analytics

    monkeypatch.setattr(state_repo, "get_state_repo", lambda: _Repo())
    monkeypatch.setattr(result_analytics, "get_credential", lambda: object())

    seen: list[tuple[str, str]] = []

    def _fake_list(_cred: Any, _account: str, *, container: str, prefix: str) -> list[Any]:
        seen.append((container, prefix))
        return []

    monkeypatch.setattr(result_analytics.storage_data, "list_result_blobs", _fake_list)

    result_analytics.list_parseable_result_blobs("stacct", job_id)

    assert seen == [
        ("results", dated),
        ("results", f"{job_id}/"),
    ]


def test_read_chain_falls_back_to_flat_when_sibling_ignored_date_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful empty dated list retries the legacy sibling write path."""
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    job_id = "c75483f2a08c"
    dated = f"2026/08/03/{job_id}/"
    flat = f"{job_id}/"

    class _Row:
        results_prefix = dated

    class _Repo:
        def get(self, _job_id: str) -> Any:
            return _Row()

    from api.services import state_repo
    from api.services.blast import result_analytics

    monkeypatch.setattr(state_repo, "get_state_repo", lambda: _Repo())
    monkeypatch.setattr(result_analytics, "get_credential", lambda: object())
    seen: list[str] = []

    def _fake_list(
        _cred: Any, _account: str, *, container: str, prefix: str
    ) -> list[dict[str, Any]]:
        assert container == "results"
        seen.append(prefix)
        if prefix == flat:
            return [{"name": f"{flat}job-elb/shard_00/batch_000.out.gz"}]
        return []

    monkeypatch.setattr(result_analytics.storage_data, "list_result_blobs", _fake_list)

    blobs = result_analytics.list_parseable_result_blobs("stacct", job_id)

    assert seen == [dated, flat]
    assert [blob["name"] for blob in blobs] == [
        f"{flat}job-elb/shard_00/batch_000.out.gz"
    ]


def test_explicit_results_prefix_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.blast import result_analytics

    monkeypatch.setattr(result_analytics, "get_credential", lambda: object())
    seen: list[str] = []
    monkeypatch.setattr(
        result_analytics.storage_data,
        "list_result_blobs",
        lambda _cred, _account, *, container, prefix: seen.append(prefix) or [],
    )

    assert (
        result_analytics.list_result_blobs_for_job(
            "stacct", "job-1", prefix="custom/job-1/"
        )
        == []
    )
    assert seen == ["custom/job-1/"]

