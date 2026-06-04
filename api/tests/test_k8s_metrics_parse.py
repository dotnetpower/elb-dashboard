"""Unit tests for Kubernetes CPU/memory quantity parsing in `api.services.k8s.metrics`.

Responsibility: Verify `_parse_cpu_millicores` / `_parse_memory_ki` handle every unit
suffix the metrics/capacity API emit (including microcores) and degrade to 0 on garbage
instead of raising, so one odd value cannot crash the AKS top-nodes snapshot refresh.
Edit boundaries: Stay focused on the two parser helpers; node/pod assembly is covered by
`test_k8s_top_pods.py`.
Key entry points: `test_parse_cpu_millicores_units`, `test_parse_cpu_millicores_invalid`,
`test_parse_memory_ki_units`.
Risky contracts: Parsers must never raise; unknown shapes return 0.
Validation: `uv run pytest -q api/tests/test_k8s_metrics_parse.py`.
"""

from __future__ import annotations

import pytest
from api.services.k8s import metrics as m


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("8", 8000),  # bare core count -> millicores
        ("0.5", 500),  # fractional cores
        ("250m", 250),  # millicores
        ("250000000n", 250),  # nanocores -> millicores
        ("102105u", 102),  # microcores -> millicores (App Insights crash repro)
        ("1500000u", 1500),
        ("", 0),
    ],
)
def test_parse_cpu_millicores_units(raw: str, expected: int) -> None:
    assert m._parse_cpu_millicores(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12x", "u", "n", " ", "1.2.3m"])
def test_parse_cpu_millicores_invalid_returns_zero(raw: str) -> None:
    assert m._parse_cpu_millicores(raw) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("1048576", 1024),  # bare bytes -> KiB
        ("2048Ki", 2048),
        ("512Mi", 512 * 1024),
        ("32Gi", 32 * 1024 * 1024),
        ("1Ti", 1024 * 1024 * 1024),
        ("", 0),
    ],
)
def test_parse_memory_ki_units(raw: str, expected: int) -> None:
    assert m._parse_memory_ki(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12Pi", "Mi", " "])
def test_parse_memory_ki_invalid_returns_zero(raw: str) -> None:
    assert m._parse_memory_ki(raw) == 0
