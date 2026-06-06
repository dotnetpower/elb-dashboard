"""Tests for the AKS prepare-db Job parameter resolver.

Responsibility: Verify the env-driven `resolve_aks_job_limits` parsing,
    clamping, and the unset-override → None contract extracted from
    `prepare_db.py` in issue #24.
Edit boundaries: Pure-function assertions only.
Key entry points: the `test_*` functions below.
Risky contracts: An unset / unparsable optional override MUST stay None so the
    downstream Job builder defaults apply.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_params.py`.
"""

from __future__ import annotations

import pytest
from api.services.storage.prepare_db_aks_params import resolve_aks_job_limits


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "PREPARE_DB_AKS_MAX_PARALLELISM",
        "PREPARE_DB_AKS_FILES_PER_POD",
        "PREPARE_DB_AKS_AZCOPY_IMAGE",
        "PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS",
        "PREPARE_DB_AKS_AZCOPY_CONCURRENCY",
        "PREPARE_DB_AKS_BACKOFF_LIMIT",
        "PREPARE_DB_AKS_TTL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults_when_unset() -> None:
    limits = resolve_aks_job_limits()
    assert limits.max_pods == 10
    assert limits.files_per_pod == 50
    assert limits.image == "mcr.microsoft.com/azure-cli:2.81.0"
    assert limits.active_deadline_seconds == 4 * 60 * 60
    # Optional overrides default to None so the builder's defaults apply.
    assert limits.azcopy_concurrency is None
    assert limits.backoff_limit is None
    assert limits.ttl_seconds_after_finished is None
    assert limits.task_overrides() == {}


def test_valid_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREPARE_DB_AKS_MAX_PARALLELISM", "20")
    monkeypatch.setenv("PREPARE_DB_AKS_FILES_PER_POD", "25")
    monkeypatch.setenv("PREPARE_DB_AKS_AZCOPY_IMAGE", "myacr.azurecr.io/azcopy:1")
    monkeypatch.setenv("PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS", "7200")
    monkeypatch.setenv("PREPARE_DB_AKS_AZCOPY_CONCURRENCY", "64")
    monkeypatch.setenv("PREPARE_DB_AKS_BACKOFF_LIMIT", "2")
    monkeypatch.setenv("PREPARE_DB_AKS_TTL_SECONDS", "600")
    limits = resolve_aks_job_limits()
    assert limits.max_pods == 20
    assert limits.files_per_pod == 25
    assert limits.image == "myacr.azurecr.io/azcopy:1"
    assert limits.active_deadline_seconds == 7200
    assert limits.task_overrides() == {
        "azcopy_concurrency": 64,
        "backoff_limit": 2,
        "ttl_seconds_after_finished": 600,
    }


def test_clamping_and_unparsable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREPARE_DB_AKS_MAX_PARALLELISM", "0")  # clamps to min 1
    monkeypatch.setenv("PREPARE_DB_AKS_FILES_PER_POD", "notanint")  # default 50
    monkeypatch.setenv("PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS", "5")  # clamps to min 60
    monkeypatch.setenv("PREPARE_DB_AKS_AZCOPY_CONCURRENCY", "9999")  # clamps to max 512
    monkeypatch.setenv("PREPARE_DB_AKS_BACKOFF_LIMIT", "bad")  # unparsable → None
    limits = resolve_aks_job_limits()
    assert limits.max_pods == 1
    assert limits.files_per_pod == 50
    assert limits.active_deadline_seconds == 60
    assert limits.azcopy_concurrency == 512
    assert limits.backoff_limit is None


def test_zero_backoff_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    # backoff_limit=0 is a valid value (no retries) and must NOT be dropped.
    monkeypatch.setenv("PREPARE_DB_AKS_BACKOFF_LIMIT", "0")
    limits = resolve_aks_job_limits()
    assert limits.backoff_limit == 0
    assert limits.task_overrides()["backoff_limit"] == 0
