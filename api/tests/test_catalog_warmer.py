"""Tests for the api-process BLAST DB catalogue warmer.

Responsibility: Prove the warmer resolves the Storage account from env
    precedence, no-ops without an account, fills the cache via
    ``list_databases_cached`` (without ``force_refresh``), degrades to a
    ``failed`` payload instead of raising, and that the start/stop lifecycle
    creates exactly one task and drains it on shutdown.
Edit boundaries: Test-only. Monkeypatches the lazily-imported
    ``list_databases_cached`` and ``get_credential`` on their source modules.
Key entry points: the test functions below.
Risky contracts: ``warm_catalog_once`` imports its dependencies at call time,
    so patch the source modules (``api.services.get_credential`` and
    ``api.services.storage.database_catalog_cache.list_databases_cached``).
Validation: `uv run pytest -q api/tests/test_catalog_warmer.py`.
"""

from __future__ import annotations

import asyncio

import api.services as services
import api.services.storage.database_catalog_cache as catalog_cache
import pytest
from api.services.storage import catalog_warmer
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_warm_once_skipped_without_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)

    result = await catalog_warmer.warm_catalog_once()

    assert result == {"status": "skipped", "reason": "no_storage_account"}


@pytest.mark.asyncio
async def test_warm_once_completes_and_does_not_force_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stwarmacct")
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.setattr(services, "get_credential", lambda: object())

    calls: dict[str, object] = {}

    def _fake_list(credential, account_name, *args, **kwargs):
        calls["account"] = account_name
        calls["force_refresh"] = kwargs.get("force_refresh")
        return [{"name": "nt"}, {"name": "nr"}, {"name": "swissprot"}]

    monkeypatch.setattr(catalog_cache, "list_databases_cached", _fake_list)

    result = await catalog_warmer.warm_catalog_once()

    assert result == {
        "status": "completed",
        "storage_account": "stwarmacct",
        "database_count": 3,
    }
    assert calls["account"] == "stwarmacct"
    # Must not force a refresh — a still-fresh cache should be reused.
    assert calls["force_refresh"] is None


@pytest.mark.asyncio
async def test_warm_once_falls_back_to_azure_storage_account_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "stlegacy")
    monkeypatch.setattr(services, "get_credential", lambda: object())
    monkeypatch.setattr(
        catalog_cache, "list_databases_cached", lambda *a, **k: [{"name": "nt"}]
    )

    result = await catalog_warmer.warm_catalog_once()

    assert result["status"] == "completed"
    assert result["storage_account"] == "stlegacy"
    assert result["database_count"] == 1


@pytest.mark.asyncio
async def test_warm_once_degrades_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stboom")
    monkeypatch.setattr(services, "get_credential", lambda: object())

    def _boom(*args, **kwargs):
        raise RuntimeError("storage unreachable")

    monkeypatch.setattr(catalog_cache, "list_databases_cached", _boom)

    result = await catalog_warmer.warm_catalog_once()

    assert result["status"] == "failed"
    assert result["storage_account"] == "stboom"
    assert "storage unreachable" in result["error"]


def test_resolve_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_DB_CATALOG_WARM_SECONDS", raising=False)
    assert catalog_warmer._resolve_interval() == 240.0


def test_resolve_interval_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_DB_CATALOG_WARM_SECONDS", "90")
    assert catalog_warmer._resolve_interval() == 90.0


def test_resolve_interval_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_DB_CATALOG_WARM_SECONDS", "abc")
    assert catalog_warmer._resolve_interval() == 240.0


@pytest.mark.asyncio
async def test_start_skips_without_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    app = FastAPI()

    catalog_warmer.start_catalog_warmer(app)

    assert getattr(app.state, catalog_warmer._STATE_ATTR, None) is None
    await catalog_warmer.stop_catalog_warmer(app)  # no-op, must not raise


@pytest.mark.asyncio
async def test_start_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stwarmacct")
    monkeypatch.setenv("BLAST_DB_CATALOG_WARM_SECONDS", "0")
    app = FastAPI()

    catalog_warmer.start_catalog_warmer(app)

    assert getattr(app.state, catalog_warmer._STATE_ATTR, None) is None


@pytest.mark.asyncio
async def test_start_stop_lifecycle_runs_at_least_one_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stwarmacct")
    # Long interval so the loop ticks once at startup then parks on the stop wait.
    monkeypatch.setenv("BLAST_DB_CATALOG_WARM_SECONDS", "3600")
    monkeypatch.setattr(services, "get_credential", lambda: object())

    ticked = asyncio.Event()

    def _fake_list(credential, account_name, *args, **kwargs):
        ticked.set()
        return [{"name": "nt"}]

    monkeypatch.setattr(catalog_cache, "list_databases_cached", _fake_list)

    app = FastAPI()
    catalog_warmer.start_catalog_warmer(app)
    state = getattr(app.state, catalog_warmer._STATE_ATTR, None)
    assert state is not None

    # The first tick should fire promptly.
    await asyncio.wait_for(ticked.wait(), timeout=2.0)

    # Second start is idempotent — no second task.
    same_state = getattr(app.state, catalog_warmer._STATE_ATTR, None)
    catalog_warmer.start_catalog_warmer(app)
    assert getattr(app.state, catalog_warmer._STATE_ATTR, None) is same_state

    await catalog_warmer.stop_catalog_warmer(app)
    task, _stop = state
    assert task.done()
    assert getattr(app.state, catalog_warmer._STATE_ATTR, None) is None
