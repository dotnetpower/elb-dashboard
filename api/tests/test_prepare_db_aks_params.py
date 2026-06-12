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
from api.services.storage.prepare_db_aks_params import (
    prefer_server_side_for_small_db,
    resolve_aks_job_limits,
)


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
        "PREPARE_DB_AKS_MIN_TOTAL_BYTES",
        "PREPARE_DB_AKS_MIN_FILE_COUNT",
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


def test_small_known_size_prefers_server_side() -> None:
    # 16S_ribosomal_RNA shape: ~18 MB across 15 files → server-side.
    assert prefer_server_side_for_small_db(18 * 1024 * 1024, 15) is True


def test_large_known_size_stays_aks() -> None:
    # core_nt shape: hundreds of GB → AKS.
    assert prefer_server_side_for_small_db(300 * 1024 * 1024 * 1024, 4800) is False


def test_unknown_size_uses_file_count_gate() -> None:
    # Sizes unknown (all 0): few files → server-side, many files → AKS so a
    # large unknown-size DB is never stranded on the slow path.
    assert prefer_server_side_for_small_db(0, 12) is True
    assert prefer_server_side_for_small_db(0, 5000) is False


def test_size_thresholds_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lower the byte threshold below 18 MB → the small DB now stays on AKS.
    monkeypatch.setenv("PREPARE_DB_AKS_MIN_TOTAL_BYTES", str(1024 * 1024))  # 1 MiB
    assert prefer_server_side_for_small_db(18 * 1024 * 1024, 15) is False
    # Raise the file-count gate so an unknown-size 5000-file DB routes server-side.
    monkeypatch.setenv("PREPARE_DB_AKS_MIN_FILE_COUNT", "9999")
    assert prefer_server_side_for_small_db(0, 5000) is True
