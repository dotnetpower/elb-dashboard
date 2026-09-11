"""Tests for the production mypy debt ratchet.

Responsibility: Verify parsing, deterministic baseline rendering, and strict
    no-change comparison inputs for ``scripts/dev/check_mypy_baseline.py``.
Edit boundaries: Pure helper tests only; do not run the full mypy suite here.
Key entry points: ``test_parse_mypy_errors``, ``test_checked_in_baseline_shape``.
Risky contracts: The baseline excludes ``api/tests`` and records production
    diagnostics by file plus mypy error code; its total is intentionally pinned
    so accidental regeneration cannot silently drop coverage.
Validation: ``uv run pytest -q api/tests/test_mypy_baseline.py``.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "dev" / "check_mypy_baseline.py"
_BASELINE_PATH = _REPO_ROOT / "scripts" / "dev" / "mypy-baseline.txt"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_mypy_baseline", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_mypy_errors_uses_stable_file_and_code_keys() -> None:
    module = _load_module()
    output = "\n".join(
        [
            'api/a.py:10: error: Item "None" has no attribute "x"  [union-attr]',
            'api/a.py:20: error: Item "None" has no attribute "y"  [union-attr]',
            "api/b.py:4: note: This is not an error",
            "api/b.py:5: error: Returning Any  [no-any-return]",
            "C:\\repo\\api\\c.py:7:3: error: Bad assignment  [assignment]",
        ]
    )

    assert module.parse_mypy_errors(output) == Counter(
        {
            ("api/a.py", "union-attr"): 2,
            ("api/b.py", "no-any-return"): 1,
            ("C:/repo/api/c.py", "assignment"): 1,
        }
    )


def test_load_baseline_rejects_duplicate_keys(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "baseline.txt"
    path.write_text("1 api/a.py arg-type\n2 api/a.py arg-type\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate mypy baseline entry"):
        module.load_baseline(path)


def test_main_fails_closed_when_error_output_is_unrecognized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_run_mypy", lambda: (Counter(), "new format", 1))

    assert module.main([]) == 2


def test_checked_in_baseline_shape() -> None:
    module = _load_module()
    baseline = module.load_baseline(_BASELINE_PATH)

    assert sum(baseline.values()) == 500
    assert len({path for path, _code in baseline}) == 97
    assert all(
        path.startswith("api/") and not path.startswith("api/tests/") for path, _ in baseline
    )
    assert module.format_baseline(baseline) == _BASELINE_PATH.read_text(encoding="utf-8")
