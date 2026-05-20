"""Cryptographically strong password generator for VM admin accounts.

Responsibility: Cryptographically strong password generator for VM admin accounts
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `generate_admin_password`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import secrets
import string

_PASSWORD_ALPHABET_UPPER = string.ascii_uppercase
_PASSWORD_ALPHABET_LOWER = string.ascii_lowercase
_PASSWORD_ALPHABET_DIGITS = string.digits
_PASSWORD_ALPHABET_SPECIAL = "!@#$%^&*()-_=+[]{}"  # noqa: S105 — alphabet, not a secret


def generate_admin_password(length: int = 24) -> str:
    """Return a password meeting Azure VM Linux complexity rules.

    Azure requires 12-72 chars with at least 3 of: uppercase, lowercase,
    digit, special. We use 24 chars and force one of each class.
    """
    if length < 16:
        raise ValueError("password length must be >= 16")

    pools = [
        _PASSWORD_ALPHABET_UPPER,
        _PASSWORD_ALPHABET_LOWER,
        _PASSWORD_ALPHABET_DIGITS,
        _PASSWORD_ALPHABET_SPECIAL,
    ]
    required = [secrets.choice(pool) for pool in pools]
    all_chars = "".join(pools)
    remaining = [secrets.choice(all_chars) for _ in range(length - len(required))]
    chars = required + remaining
    # Shuffle deterministically using secrets — Fisher-Yates with secrets.randbelow.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)
