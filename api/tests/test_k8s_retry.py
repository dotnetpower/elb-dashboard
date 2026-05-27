"""Tests for the pooled Kubernetes session retry adapter.

Responsibility: Pin the urllib3 Retry configuration used by the pooled
session so a future refactor cannot silently turn off connect-time DNS
retries (which would re-introduce the App Insights noise from transient
Container Apps coredns hiccups).

Edit boundaries: Pure unit-level — no real K8s, no real ARM, no real DNS.

Key entry points: `test_*`.

Risky contracts: Retry MUST only cover connect failures, never HTTP status
codes — the API server's 4xx/5xx is its authoritative answer.

Validation: `uv run pytest -q api/tests/test_k8s_retry.py`.
"""

from __future__ import annotations

import pytest
from api.services.k8s import client as k8s_client


def test_build_k8s_retry_default_only_retries_connect_failures() -> None:
    retry = k8s_client._build_k8s_retry()

    assert retry.total == k8s_client._K8S_SESSION_RETRY_TOTAL
    assert retry.connect == k8s_client._K8S_SESSION_RETRY_TOTAL
    # Reads / statuses / redirects are NOT retried — the API server's
    # response is authoritative.
    assert retry.read == 0
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
