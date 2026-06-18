"""Tests for the pooled Kubernetes session retry adapter.

Responsibility: Pin the urllib3 Retry configuration used by the pooled
session so a future refactor cannot silently turn off the transient
transport retries (connect-time DNS hiccups AND read-time keepalive
aborts) that would otherwise re-introduce App Insights noise from
transient Container Apps coredns / dropped-socket events.

Edit boundaries: Pure unit-level — no real K8s, no real ARM, no real DNS.

Key entry points: `test_*`.

Risky contracts: Retry MUST cover transport-level failures (connect + read)
but never HTTP status codes — the API server's 4xx/5xx is its authoritative
answer — and only for idempotent verbs.

Validation: `uv run pytest -q api/tests/test_k8s_retry.py`.
"""

from __future__ import annotations

import pytest
from api.services.k8s import client as k8s_client


def test_build_k8s_retry_default_retries_transport_failures() -> None:
    retry = k8s_client._build_k8s_retry()

    assert retry.total == k8s_client._K8S_SESSION_RETRY_TOTAL
    assert retry.connect == k8s_client._K8S_SESSION_RETRY_TOTAL
    # Read aborts (dropped keepalive / RemoteDisconnected) ARE retried now —
    # urllib3 classifies a ProtocolError as a read error, so read=0 made the
    # noisiest App Insights exception terminal. Statuses / redirects stay 0.
    assert retry.read == k8s_client._K8S_SESSION_RETRY_TOTAL
    assert retry.status == 0
    assert retry.redirect == 0
    assert retry.other == 0
    assert retry.backoff_factor == k8s_client._K8S_SESSION_RETRY_BACKOFF
    # No HTTP status forces a retry — every 4xx/5xx is surfaced directly.
    assert tuple(retry.status_forcelist or ()) == ()
    # Only idempotent verbs (GET/HEAD/OPTIONS) are retried so a half-
    # delivered POST/PATCH never doubles up.
    assert retry.allowed_methods == frozenset(["GET", "HEAD", "OPTIONS"])
    assert retry.raise_on_status is False
    assert retry.raise_on_redirect is False


def test_build_k8s_retry_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_SESSION_RETRY_TOTAL", "3")
    monkeypatch.setenv("K8S_SESSION_RETRY_BACKOFF", "1.5")

    retry = k8s_client._build_k8s_retry()

    assert retry.total == 3
    assert retry.connect == 3
    assert retry.backoff_factor == 1.5


def test_build_k8s_retry_caps_pathological_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence against an operator setting an absurd retry count that would
    keep the worker stuck on a real outage."""
    monkeypatch.setenv("K8S_SESSION_RETRY_TOTAL", "9999")
    monkeypatch.setenv("K8S_SESSION_RETRY_BACKOFF", "9999")

    retry = k8s_client._build_k8s_retry()

    assert retry.total == 5  # capped
    assert retry.backoff_factor == 5.0  # capped


def test_build_k8s_retry_ignores_bogus_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_SESSION_RETRY_TOTAL", "not-a-number")
    monkeypatch.setenv("K8S_SESSION_RETRY_BACKOFF", "not-a-float")

    retry = k8s_client._build_k8s_retry()

    assert retry.total == k8s_client._K8S_SESSION_RETRY_TOTAL
    assert retry.backoff_factor == k8s_client._K8S_SESSION_RETRY_BACKOFF


def test_remote_disconnected_on_get_is_retried_not_terminal() -> None:
    """Regression for the warmup pod-log fan-out (#45).

    The AKS API server silently drops idle pooled keepalive sockets; the next
    GET on that dead socket raises
    ``ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected))``.
    urllib3 classifies a ``ProtocolError`` as a READ error, so the previous
    ``read=0`` config made the very first abort terminal and recorded an App
    Insights exception. With read retries enabled, the first abort on an
    idempotent GET must be retried (``increment`` returns a new Retry) rather
    than raising ``MaxRetryError``.
    """
    from urllib3.exceptions import MaxRetryError, ProtocolError

    retry = k8s_client._build_k8s_retry()
    abort = ProtocolError(
        "Connection aborted.",
        OSError("Remote end closed connection without response"),
    )

    # First read abort on a GET is retryable (does not raise).
    after_first = retry.increment(
        method="GET",
        url="/api/v1/namespaces/default/pods/warm-core-nt-09-abc/log",
        error=abort,
    )
    assert after_first.read == 0  # one read budget consumed, not negative

    # The single retry budget is bounded: a second abort exhausts it.
    with pytest.raises(MaxRetryError):
        after_first.increment(
            method="GET",
            url="/api/v1/namespaces/default/pods/warm-core-nt-09-abc/log",
            error=ProtocolError("Connection aborted.", OSError("again")),
        )


def test_remote_disconnected_on_post_is_not_retried() -> None:
    """A dropped socket on a NON-idempotent verb must never be replayed.

    ``allowed_methods`` is GET/HEAD/OPTIONS only, so a read abort on a POST
    (e.g. a k8s deployment patch) exhausts immediately rather than re-sending
    the mutation. urllib3 re-raises the ORIGINAL transport error (not a
    ``MaxRetryError``) for a non-retryable method.
    """
    from urllib3.exceptions import ProtocolError

    retry = k8s_client._build_k8s_retry()
    with pytest.raises(ProtocolError):
        retry.increment(
            method="POST",
            url="/apis/apps/v1/namespaces/default/deployments/elb-openapi",
            error=ProtocolError("Connection aborted.", OSError("closed")),
        )
