"""Tests for the password generator."""

from __future__ import annotations

import string

import pytest

from api.services.passwords import generate_admin_password


def test_generate_admin_password_length() -> None:
    assert len(generate_admin_password(24)) == 24


def test_generate_admin_password_includes_all_classes() -> None:
    password = generate_admin_password(24)
    assert any(c in string.ascii_uppercase for c in password)
    assert any(c in string.ascii_lowercase for c in password)
    assert any(c in string.digits for c in password)
    assert any(c in "!@#$%^&*()-_=+[]{}" for c in password)


def test_generate_admin_password_rejects_short() -> None:
    with pytest.raises(ValueError):
        generate_admin_password(8)


def test_generate_admin_password_unique() -> None:
    values = {generate_admin_password(24) for _ in range(50)}
    assert len(values) == 50
