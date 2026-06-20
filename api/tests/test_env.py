"""Unit tests for the shared `env_int` parsing helper.

Responsibility: Lock the fallback + clamp contract of `api.services.env.env_int`.
Edit boundaries: Pure helper tests; no Azure / network.
Key entry points: the test functions below.
Risky contracts: `env_int` must never raise and must fall back to `default`
for unset / empty / unparseable input.
Validation: `uv run pytest -q api/tests/test_env.py`.
"""

from __future__ import annotations

import pytest
from api.services.env import env_int


def test_env_int_unset_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELB_TEST_KNOB", raising=False)
    assert env_int("ELB_TEST_KNOB", 42) == 42


def test_env_int_empty_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELB_TEST_KNOB", "")
    assert env_int("ELB_TEST_KNOB", 42) == 42


def test_env_int_unparseable_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELB_TEST_KNOB", "not-a-number")
    assert env_int("ELB_TEST_KNOB", 7) == 7


def test_env_int_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELB_TEST_KNOB", "123")
    assert env_int("ELB_TEST_KNOB", 7) == 123


def test_env_int_clamps_to_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELB_TEST_KNOB", "-5")
    assert env_int("ELB_TEST_KNOB", 7, minimum=1) == 1


def test_env_int_clamps_to_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELB_TEST_KNOB", "1000")
    assert env_int("ELB_TEST_KNOB", 7, minimum=1, maximum=100) == 100


def test_env_int_no_clamp_without_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELB_TEST_KNOB", "-5")
    assert env_int("ELB_TEST_KNOB", 7) == -5
