"""Tests for the terminal sidecar entrypoint wiring."""

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
