"""Tests for the bounded, paged Kubernetes LIST helper.

Responsibility: Pin the memory-safety contract that stopped the api sidecar OOM
    (exit 137) — every cluster-wide LIST is paged with an explicit ``limit``, the
    total item count is capped, the page loop is bounded, an expired continue
    token degrades to a partial result, and the concurrency gate sheds load
    instead of stalling.
Edit boundaries: Exercises the transport loop with a fake session; no live
    Kubernetes API and no credential plumbing.
Key entry points: the ``test_*`` functions.
Risky contracts: the page loop must terminate for ANY server behaviour,
    including one that always returns a continue token.
Validation: ``uv run pytest -q api/tests/test_k8s_paged_list.py``.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from api.services.k8s import paged_list


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("K8S_LIST_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("K8S_LIST_PAGE_LIMIT", raising=False)
    monkeypatch.delenv("K8S_LIST_MAX_ITEMS", raising=False)
    paged_list._reset_gate_for_tests()
    yield
    paged_list._reset_gate_for_tests()


class _Resp:
    def __init__(self, payload: dict[str, Any], *, status: int = 200, size: int = 1024) -> None:
        self._payload = payload
        self.status_code = status
        self.headers = {"content-length": str(size)}
        self.content = b"x" * 0

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status at {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    """Fake session that hands back a scripted sequence of pages."""

    def __init__(self, pages: list[_Resp]) -> None:
        self.pages = pages
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> _Resp:
        self.calls.append(dict(params))
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


def _page(names: list[str], continue_token: str = "") -> _Resp:
    return _Resp(
        {
            "items": [{"metadata": {"name": n}} for n in names],
            "metadata": ({"continue": continue_token} if continue_token else {}),
        }
    )


def test_every_request_carries_an_explicit_limit() -> None:
    """The whole point: never issue an unpaged cluster-wide LIST again."""
    session = _Session([_page(["a", "b"])])

    paged_list.list_k8s_items(session, "https://k8s/api/v1/pods", label="pods")

    assert session.calls and all("limit" in call for call in session.calls)


def test_pages_through_continue_tokens_and_concatenates() -> None:
    session = _Session([_page(["a"], "tok1"), _page(["b"], "tok2"), _page(["c"])])

    items = paged_list.list_k8s_items(session, "https://k8s/api/v1/pods", label="pods")

    assert [i["metadata"]["name"] for i in items] == ["a", "b", "c"]
    # Page 2 and 3 must forward the token they were given.
    assert session.calls[1]["continue"] == "tok1"
    assert session.calls[2]["continue"] == "tok2"
    assert "continue" not in session.calls[0]


def test_total_items_are_capped_and_truncation_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _Session([_page([f"p{i}" for i in range(50)], "more")])

    with caplog.at_level("WARNING", logger=paged_list.LOGGER.name):
        items = paged_list.list_k8s_items(
            session, "https://k8s/api/v1/pods", label="pods", max_items=10
        )

    assert len(items) == 10
    # Silent truncation would look like data loss to an operator; it must log.
    assert any("truncated" in r.message for r in caplog.records)


def test_page_loop_terminates_when_the_server_always_returns_a_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Liveness: a server (or a bug) that never stops paging must not hang."""
    session = _Session([_page(["a"], "never-ends")])

    with caplog.at_level("WARNING", logger=paged_list.LOGGER.name):
        items = paged_list.list_k8s_items(
            session, "https://k8s/api/v1/pods", label="pods", max_items=100_000
        )

    assert len(session.calls) == paged_list._MAX_PAGES
    assert len(items) == paged_list._MAX_PAGES
    assert any("page ceiling" in r.message for r in caplog.records)


def test_expired_continue_token_degrades_to_a_partial_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 410 mid-pagination must not blank the whole monitoring card."""
    session = _Session([_page(["a"], "tok"), _Resp({}, status=410)])

    with caplog.at_level("WARNING", logger=paged_list.LOGGER.name):
        items = paged_list.list_k8s_items(session, "https://k8s/api/v1/pods", label="pods")

    assert [i["metadata"]["name"] for i in items] == ["a"]
    assert any("continue token expired" in r.message for r in caplog.records)


def test_oversized_page_body_is_warned(caplog: pytest.LogCaptureFixture) -> None:
    """Next investigation should read the size off the log, not tracemalloc."""
    big = _page(["a"])
    big.headers = {"content-length": str(paged_list._LARGE_PAGE_WARN_BYTES + 1)}
    session = _Session([big])

    with caplog.at_level("WARNING", logger=paged_list.LOGGER.name):
        paged_list.list_k8s_items(session, "https://k8s/api/v1/pods", label="pods")

    assert any("unusually large" in r.message for r in caplog.records)


def test_concurrency_gate_sheds_instead_of_stalling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full gate raises promptly; an unbounded wait would stall the workers."""
    monkeypatch.setenv("K8S_LIST_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(paged_list, "_ACQUIRE_TIMEOUT_SECONDS", 0.05)
    paged_list._reset_gate_for_tests()
    held = threading.Event()
    release = threading.Event()

    class _Blocking(_Session):
        def get(self, url: str, *, params: dict[str, str], timeout: float) -> _Resp:
            held.set()
            release.wait(timeout=5)
            return super().get(url, params=params, timeout=timeout)

    blocker = _Blocking([_page(["a"])])
    thread = threading.Thread(
        target=lambda: paged_list.list_k8s_items(blocker, "https://k8s/x", label="pods"),
        daemon=True,
    )
    thread.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(paged_list.K8sListBusy):
            paged_list.list_k8s_items(_Session([_page(["b"])]), "https://k8s/y", label="pods")
    finally:
        release.set()
        thread.join(timeout=5)


def test_gate_is_released_even_when_the_session_raises() -> None:
    """A leaked permit would wedge every later LIST behind a full gate."""

    class _Boom(_Session):
        def get(self, url: str, *, params: dict[str, str], timeout: float) -> _Resp:
            raise RuntimeError("transport down")

    with pytest.raises(RuntimeError):
        paged_list.list_k8s_items(_Boom([]), "https://k8s/x", label="pods")

    # The gate must be free again — a second call proceeds normally.
    items = paged_list.list_k8s_items(_Session([_page(["a"])]), "https://k8s/y", label="pods")
    assert len(items) == 1
