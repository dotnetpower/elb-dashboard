"""Unit-level contract tests for the fast-fail helpers shipped 2026-06-27.

Responsibility: Lock the *shape* of the two probe helpers introduced after
    the kombu / Service Bus "unbounded TCP connect" audit
    (commits 5a9cef6 and 2300af7). The integration paths
    (`test_smoke.py::readiness`, `test_blast_submit_gates.py`,
    `test_service_bus_*`) exercise the helpers indirectly, but none of them
    assert *that the right kwarg name is set*. A future refactor that
    renamed (e.g.) ``socket_connect_timeout`` to ``socket_conect_timeout``
    would land green via the integration suites because the test brokers
    are mocked or use the redis fast-fail wrapper — and silently
    re-introduce the production hang in
    `docs/research/unbounded-socket-timeouts.md`.
Edit boundaries: contract assertions only — no integration plumbing.
Key entry points: `test_fast_probe_connection_sets_socket_connect_timeout`,
    `test_fast_probe_connection_preserves_existing_transport_options`,
    `test_fast_probe_connection_socket_connect_timeout_override`,
    `test_sb_client_kwargs_defaults_match_research_doc`,
    `test_sb_client_kwargs_env_overrides`.
Risky contracts: The kwarg names asserted here are dictated by the
    upstream SDKs (kombu redis transport, azure-servicebus). If those
    libraries rename their knobs we must update the helpers AND these
    tests in lock-step.
Validation: ``uv run pytest -q api/tests/test_fast_probe_helpers.py``.
"""

from __future__ import annotations

import pytest


def test_fast_probe_connection_sets_socket_connect_timeout() -> None:
    """The helper must put ``socket_connect_timeout`` on the kombu connection.

    This is the load-bearing assertion: without that key the inner
    ``sock.connect()`` inherits the OS default (75-120 s on Linux) and the
    readiness / pre-flight / submit-gate probes block past every caller
    deadline on a filtered broker port. See
    docs/research/unbounded-socket-timeouts.md for the full chain.
    """
    from api.celery_app import fast_probe_connection

    conn = fast_probe_connection()
    try:
        assert "socket_connect_timeout" in conn.transport_options
        # Default is 2 s per the helper signature; the absolute value matters
        # less than "is bounded at all" but we still pin it so a careless
        # change to e.g. 60 s is caught.
        assert conn.transport_options["socket_connect_timeout"] == 2
    finally:
        try:
            conn.close()
        except Exception:  # noqa: S110 - close() is best-effort here
            pass


def test_fast_probe_connection_preserves_existing_transport_options() -> None:
    """If kombu (or a future caller) has already set transport_options we must
    merge, not clobber. A `del`-like overwrite would silently drop SSL /
    keepalive / heartbeat tuning supplied elsewhere."""
    from api.celery_app import celery_app, fast_probe_connection

    # Build a connection the normal way and seed an unrelated transport option.
    seed = celery_app.connection()
    existing = dict(seed.transport_options or {})
    seed.close()
    existing["fanout_prefix"] = True  # arbitrary unrelated kombu option

    # Monkey the celery app's connection() to return a stub that exposes the
    # seeded options, so we exercise the merge branch deterministically.
    class _Stub:
        def __init__(self) -> None:
            self.transport_options = dict(existing)

    import api.celery_app as celery_module

    orig = celery_module.celery_app.connection
    celery_module.celery_app.connection = lambda *_a, **_kw: _Stub()  # type: ignore[assignment]
    try:
        conn = fast_probe_connection()
        assert conn.transport_options.get("fanout_prefix") is True
        assert conn.transport_options.get("socket_connect_timeout") == 2
    finally:
        celery_module.celery_app.connection = orig  # type: ignore[assignment]


def test_fast_probe_connection_socket_connect_timeout_override() -> None:
    """The helper accepts an explicit override — verifies the signature, not
    the default."""
    from api.celery_app import fast_probe_connection

    conn = fast_probe_connection(socket_connect_timeout=7.5)
    try:
        assert conn.transport_options["socket_connect_timeout"] == 7.5
    finally:
        try:
            conn.close()
        except Exception:  # noqa: S110
            pass


def test_sb_client_kwargs_defaults_match_research_doc() -> None:
    """Defaults are the values documented in
    docs/features_change/2026-06/2026-06-27-servicebus-retry-cap.md and
    docs/research/unbounded-socket-timeouts.md. Drift here means the docs
    lie."""
    from api.services.service_bus import _sb_client_kwargs

    kwargs = _sb_client_kwargs()
    assert kwargs["retry_total"] == 3
    assert kwargs["retry_backoff_max"] == 30


def test_sb_client_kwargs_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """SERVICEBUS_RETRY_TOTAL / SERVICEBUS_RETRY_BACKOFF_MAX must take effect
    without a code change, so an operator can relax for a high-latency
    deployment from the Container App env."""
    monkeypatch.setenv("SERVICEBUS_RETRY_TOTAL", "5")
    monkeypatch.setenv("SERVICEBUS_RETRY_BACKOFF_MAX", "75.5")

    from api.services.service_bus import _sb_client_kwargs

    kwargs = _sb_client_kwargs()
    assert kwargs["retry_total"] == 5
    assert kwargs["retry_backoff_max"] == 75.5
