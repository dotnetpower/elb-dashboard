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
