"""Tests for the browser terminal login banner renderer.

Responsibility: Tests for the browser terminal login banner renderer
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_banner_renders_plain_fallback_without_tty`,
`test_banner_can_render_colour_prompt_for_xterm`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_banner.py`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BANNER = REPO_ROOT / "terminal" / "banner.sh"


def test_banner_renders_plain_fallback_without_tty() -> None:
    result = subprocess.run(  # noqa: S603 - test executes a static bash script.
        ["/bin/bash", str(BANNER)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "NO_COLOR": "1",
            "ELB_TERMINAL_MOTD_PATH": str(REPO_ROOT / "terminal" / "motd"),
        },
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Elastic" in result.stdout
    assert "Blast" in result.stdout
    assert "CLI" in result.stdout
    assert "██" in result.stdout
    assert "▄▄▄▄" in result.stdout
    assert "Signed in with Azure" in result.stdout
    assert "Guard: protected shell" in result.stdout
    assert "Audit:" in result.stdout
    assert "~/.elb-history/commands.<pid>.log" in result.stdout
    assert "az login --identity" not in result.stdout
    assert "az login --use-device-code" not in result.stdout
    assert "terminal-home Azure Files" not in result.stdout
    assert "\x1b[" not in result.stdout


def test_banner_can_render_colour_prompt_for_xterm() -> None:
    result = subprocess.run(  # noqa: S603 - test executes a static bash script.
        ["/bin/bash", str(BANNER)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "TERM": "xterm-256color",
            "ELB_TERMINAL_BANNER_FORCE_COLOR": "1",
            "ELB_TERMINAL_BANNER": "static",
        },
        text=True,
        capture_output=True,
        check=True,
    )

    assert "██" in result.stdout
    assert "▄▄▄▄" in result.stdout
    assert "\x1b[38;2;" in result.stdout
    assert "\x1b[38;5;51m" in result.stdout
    assert "\x1b[38;5;201m" in result.stdout
    assert "\x1b[48;5;" not in result.stdout
    assert "Elastic" in result.stdout
    assert "Blast" in result.stdout
    assert "CLI" in result.stdout
    assert "Signed in with Azure" in result.stdout
    assert "Guard:" in result.stdout
    assert "browser" in result.stdout
    assert ">>>" in result.stdout
    assert "Audit:" in result.stdout
    assert "~/.elb-history/commands.<pid>.log" in result.stdout
    assert "az login --use-device-code" not in result.stdout
