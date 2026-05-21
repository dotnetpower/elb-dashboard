"""Tests for terminal runtime ElasticBLAST overrides.

Responsibility: Tests for terminal runtime ElasticBLAST overrides
Edit boundaries: Keep tests isolated from the real elastic-blast package and Azure runtime.
Key entry points: `_load_sitecustomize`,
`test_fast_json_submit_cleanup_override_clears_success_stack`,
`test_fast_json_submit_cleanup_override_keeps_failure_stack`.
Risky contracts: Do not import or mutate the real sibling elastic-blast-azure checkout.
Validation: `uv run pytest -q api/tests/test_terminal_runtime_overrides.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any


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
