"""Tests for the per-operator browser-terminal tmux session token.

Responsibility: Lock down ``_session_arg`` so the terminal proxy keeps each
    operator on their own tmux session (issue #2 in the security-audit
    follow-up) — deterministic per owner, distinct across owners, and using
    only the ``[a-z0-9]`` charset the ttyd `elb-tmux-attach` wrapper accepts.
    Also lock the argv boundary in ``_build_upstream_url`` so no
    browser-controlled value can reach the ttyd `?arg=`.
Edit boundaries: Pure-function unit test — no WebSocket / network needed.
Key entry points: ``test_token_is_deterministic_per_owner``,
    ``test_token_differs_across_owners``, ``test_token_charset_safe``,
    ``test_missing_owner_falls_back_without_crashing``,
    ``test_upstream_url_only_carries_derived_arg``,
    ``test_upstream_url_arg_matches_session_token``.
Risky contracts: The token must never embed the raw OID and must stay stable
    so a browser refresh re-attaches the same session; the upstream URL must
    derive its `arg` solely from the server-side owner_oid.
Validation: ``uv run pytest -q api/tests/test_terminal_session_arg.py``.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import api.routes.terminal.ws as ws_module

_OWNER_A = "11111111-2222-3333-4444-555555555555"
_OWNER_B = "99999999-8888-7777-6666-555555555555"


def test_token_is_deterministic_per_owner() -> None:
    # Same operator → same session token, so a browser refresh re-attaches
    # the operator's own tmux session instead of spawning a fresh one.
    assert ws_module._session_arg(_OWNER_A) == ws_module._session_arg(_OWNER_A)


def test_token_differs_across_owners() -> None:
    # Two distinct operators must never collide onto a shared session.
    assert ws_module._session_arg(_OWNER_A) != ws_module._session_arg(_OWNER_B)


def test_token_charset_safe() -> None:
    token = ws_module._session_arg(_OWNER_A)
    # ttyd forwards this verbatim to the wrapper; keep it to the charset the
    # wrapper accepts and short enough to stay under tmux's name limits.
    assert re.fullmatch(r"u[0-9a-f]{16}", token)
    # Defence-in-depth: the raw OID must not leak onto the ttyd command line.
    assert _OWNER_A not in token


def test_missing_owner_falls_back_without_crashing() -> None:
    # Dev-bypass / missing identity must still yield a valid, stable token.
    token = ws_module._session_arg(None)
    assert re.fullmatch(r"u[0-9a-f]{16}", token)
    assert token == ws_module._session_arg(None)


def test_upstream_url_only_carries_derived_arg() -> None:
    # The upstream ttyd URL must expose exactly ONE query parameter — `arg` —
    # and nothing else. If a future change let the browser's `?ticket=` (or any
    # other client-controlled value) flow into this URL, a caller could attach
    # to an arbitrary or a known victim's tmux session. This is the argv
    # boundary that the whole isolation property rests on.
    url = ws_module._build_upstream_url(_OWNER_A)
    parts = urlsplit(url)
    assert parts.path.endswith("/ws")
    qs = parse_qs(parts.query)
    assert set(qs.keys()) == {"arg"}
    assert qs["arg"] == [ws_module._session_arg(_OWNER_A)]


def test_upstream_url_arg_matches_session_token() -> None:
    # Two different owners produce two different upstream URLs; the same owner
    # always produces the same one (refresh re-attach).
    assert ws_module._build_upstream_url(_OWNER_A) == ws_module._build_upstream_url(_OWNER_A)
    assert ws_module._build_upstream_url(_OWNER_A) != ws_module._build_upstream_url(_OWNER_B)
    # The raw OID never appears in the URL.
    assert _OWNER_A not in ws_module._build_upstream_url(_OWNER_A)
