"""Tests for the terminal sidecar entrypoint wiring.

Responsibility: Tests for the terminal sidecar entrypoint wiring
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_ttyd_attaches_to_persistent_tmux_session`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_entrypoint.py`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRYPOINT = REPO_ROOT / "terminal" / "entrypoint.sh"


def test_ttyd_attaches_to_persistent_tmux_session() -> None:
    body = ENTRYPOINT.read_text()

    assert "/usr/local/bin/ttyd" in body
    assert "/usr/bin/tmux new-session -A -s elb /bin/bash --login" in body
    assert "new-session -A -D" not in body
    assert "  /bin/bash --login &" not in body
