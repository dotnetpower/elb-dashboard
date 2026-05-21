"""Tests for terminal runtime ElasticBLAST overrides.

Responsibility: Tests for terminal runtime ElasticBLAST overrides
Edit boundaries: Keep tests isolated from the real elastic-blast package and Azure runtime.
Key entry points: `_load_sitecustomize`, `_load_sitecustomize_with_fast_azure_io`,
`test_fast_json_submit_cleanup_override_clears_success_stack`,
`test_fast_json_submit_cleanup_override_keeps_failure_stack`,
`test_fast_azure_io_uses_blob_sdk_for_length_and_upload`,
`test_fast_azure_io_uses_blob_sdk_for_db_presence`.
Risky contracts: Do not import or mutate the real sibling elastic-blast-azure checkout.
Validation: `uv run pytest -q api/tests/test_terminal_runtime_overrides.py`.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _load_sitecustomize(monkeypatch: Any, return_code: int = 0) -> tuple[ModuleType, list[str]]:
    calls: list[str] = []
    elastic_blast = ModuleType("elastic_blast")
    azure_cli_glue = ModuleType("elastic_blast.azure_cli_glue")

    def original_submit_command(
        args: Any,
        cfg: Any,
        clean_up_stack: list[Callable[..., Any]],
        *,
        default_submit: Callable[..., int],
    ) -> int:
        del args, cfg, clean_up_stack, default_submit
        calls.append("submit")
        return return_code

    azure_cli_glue.submit_command = original_submit_command  # type: ignore[attr-defined]
    elastic_blast.azure_cli_glue = azure_cli_glue  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "elastic_blast", elastic_blast)
    monkeypatch.setitem(sys.modules, "elastic_blast.azure_cli_glue", azure_cli_glue)
    monkeypatch.setenv("ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP", "1")

    module_path = (
        Path(__file__).resolve().parents[2] / "terminal" / "runtime_overrides" / "sitecustomize.py"
    )
    spec = importlib.util.spec_from_file_location("terminal_runtime_sitecustomize", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return azure_cli_glue, calls


def _exec_sitecustomize() -> ModuleType:
    module_path = (
        Path(__file__).resolve().parents[2] / "terminal" / "runtime_overrides" / "sitecustomize.py"
    )
    spec = importlib.util.spec_from_file_location("terminal_runtime_sitecustomize", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_sitecustomize_with_fast_azure_io(monkeypatch: Any) -> tuple[ModuleType, ModuleType, dict]:
    fake_state: dict[str, Any] = {"uploads": [], "fallbacks": []}

    elastic_blast = ModuleType("elastic_blast")
    constants = ModuleType("elastic_blast.constants")
    constants.ELB_AZURE_PREFIX = "https://"  # type: ignore[attr-defined]

    filehelper = ModuleType("elastic_blast.filehelper")
    filehelper.re = re  # type: ignore[attr-defined]

    def fallback(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        fake_state["fallbacks"].append("called")

    @contextmanager
    def fallback_writer(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        fake_state["fallbacks"].append("write")
        yield SimpleNamespace(write=lambda value: value)

    filehelper.get_length = fallback  # type: ignore[attr-defined]
    filehelper.check_for_read = fallback  # type: ignore[attr-defined]
    filehelper.open_for_read = fallback  # type: ignore[attr-defined]
    filehelper.open_for_write_immediate = fallback_writer  # type: ignore[attr-defined]
    filehelper.unpack_stream = lambda stream, gzipped, tarred: stream  # type: ignore[attr-defined]

    util = ModuleType("elastic_blast.util")
    util.check_user_provided_blastdb_exists = fallback  # type: ignore[attr-defined]
    util.get_blastdb_info = fallback  # type: ignore[attr-defined]
    util.sanitize_for_k8s = lambda value: value.replace("_", "-")  # type: ignore[attr-defined]

    class FakeBlobClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        @classmethod
        def from_blob_url(cls, blob_url: str) -> FakeBlobClient:
            return cls(blob_url=blob_url)

        def get_blob_properties(self) -> dict[str, int]:
            return {"size": 123}

        def download_blob(self) -> SimpleNamespace:
            return SimpleNamespace(readall=lambda: b"hello")

        def upload_blob(self, data: bytes, overwrite: bool) -> None:
            fake_state["uploads"].append((data, overwrite))

    class FakeContainerClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        @classmethod
        def from_container_url(cls, container_url: str) -> FakeContainerClient:
            return cls(container_url=container_url)

        def list_blobs(self, name_starts_with: str) -> list[SimpleNamespace]:
            fake_state["list_prefix"] = name_starts_with
            return [SimpleNamespace(name=f"{name_starts_with}.nsq")]

    azure = ModuleType("azure")
    azure_identity = ModuleType("azure.identity")
    azure_identity.AzureCliCredential = lambda: object()  # type: ignore[attr-defined]
    azure_identity.ManagedIdentityCredential = lambda client_id=None: object()  # type: ignore[attr-defined]
    azure_identity.ChainedTokenCredential = lambda *credentials: credentials  # type: ignore[attr-defined]
    azure_storage = ModuleType("azure.storage")
    azure_blob = ModuleType("azure.storage.blob")
    azure_blob.BlobClient = FakeBlobClient  # type: ignore[attr-defined]
    azure_blob.ContainerClient = FakeContainerClient  # type: ignore[attr-defined]

    elastic_blast.filehelper = filehelper  # type: ignore[attr-defined]
    elastic_blast.util = util  # type: ignore[attr-defined]
    elastic_blast.constants = constants  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "elastic_blast", elastic_blast)
    monkeypatch.setitem(sys.modules, "elastic_blast.filehelper", filehelper)
    monkeypatch.setitem(sys.modules, "elastic_blast.util", util)
    monkeypatch.setitem(sys.modules, "elastic_blast.constants", constants)
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", azure_identity)
    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_blob)
    monkeypatch.setenv("ELB_DASHBOARD_FAST_AZURE_IO", "1")

    _exec_sitecustomize()
    return filehelper, util, fake_state


def test_fast_json_submit_cleanup_override_clears_success_stack(monkeypatch: Any) -> None:
    azure_cli_glue, calls = _load_sitecustomize(monkeypatch, return_code=0)
    stack: list[Callable[..., Any]] = [lambda: None]

    return_code = azure_cli_glue.submit_command(  # type: ignore[attr-defined]
        SimpleNamespace(json=True),
        object(),
        stack,
        default_submit=lambda: 0,
    )

    assert return_code == 0
    assert calls == ["submit"]
    assert stack == []


def test_fast_json_submit_cleanup_override_keeps_failure_stack(monkeypatch: Any) -> None:
    azure_cli_glue, calls = _load_sitecustomize(monkeypatch, return_code=42)

    def cleanup() -> None:
        return None

    stack: list[Callable[..., Any]] = [cleanup]

    return_code = azure_cli_glue.submit_command(  # type: ignore[attr-defined]
        SimpleNamespace(json=True),
        object(),
        stack,
        default_submit=lambda: 0,
    )

    assert return_code == 42
    assert calls == ["submit"]
    assert stack == [cleanup]


def test_fast_azure_io_uses_blob_sdk_for_length_and_upload(monkeypatch: Any) -> None:
    filehelper, _util, fake_state = _load_sitecustomize_with_fast_azure_io(monkeypatch)
    blob_url = "https://acct.blob.core.windows.net/queries/job/query.fa"

    assert filehelper.get_length(blob_url) == 123  # type: ignore[attr-defined]
    filehelper.check_for_read(blob_url, print_file_size=True)  # type: ignore[attr-defined]
    assert filehelper.open_for_read(blob_url).read() == "hello"  # type: ignore[attr-defined]
    with filehelper.open_for_write_immediate(blob_url) as handle:  # type: ignore[attr-defined]
        handle.write("abc")

    assert fake_state["uploads"] == [(b"abc", True)]
    assert fake_state["fallbacks"] == []


def test_fast_azure_io_uses_blob_sdk_for_db_presence(monkeypatch: Any) -> None:
    _filehelper, util, fake_state = _load_sitecustomize_with_fast_azure_io(monkeypatch)
    db_url = "https://acct.blob.core.windows.net/blastdb/core_nt"

    util.check_user_provided_blastdb_exists(db_url, object(), object())  # type: ignore[attr-defined]
    db_name, db_path, label = util.get_blastdb_info(db_url)  # type: ignore[attr-defined]

    assert fake_state["list_prefix"] == "core_nt"
    assert db_name == "core_nt"
    assert db_path == "https://acct.blob.core.windows.net/blastdb/*"
    assert label == "core-nt"
    assert fake_state["fallbacks"] == []


def test_fast_azure_io_raises_when_db_is_missing(monkeypatch: Any) -> None:
    _filehelper, util, _fake_state = _load_sitecustomize_with_fast_azure_io(monkeypatch)

    from azure.storage.blob import ContainerClient

    ContainerClient.list_blobs = lambda self, name_starts_with: []  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="was not found"):
        util.check_user_provided_blastdb_exists(  # type: ignore[attr-defined]
            "https://acct.blob.core.windows.net/blastdb/missing", object(), object()
        )
