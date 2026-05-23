"""Tests for the interactive terminal shell command guard.

Responsibility: Tests for the interactive terminal shell command guard
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_guard_check`, `test_guard_trap_blocks_before_execution`,
`test_guard_allows_non_destructive_rm`, `test_guard_blocks_recursive_home_delete`,
`test_guard_blocks_recursive_workspace_wipe`, `test_guard_blocks_azure_delete_operations`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_command_guard.py`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = REPO_ROOT / "terminal" / "command_guard.sh"


def _guard_check(command_text: str) -> subprocess.CompletedProcess[str]:
    # Pass the command as $1 (argv) instead of inlining it into the
    # script body — that way bash's quote/escape rules don't mangle
    # special chars like backslash before sudo.
    script = f"""
set -euo pipefail
source {GUARD}
if __elb_terminal_command_allowed "$1"; then
  echo allowed
else
  echo blocked
fi
"""
    return subprocess.run(  # noqa: S603 - test executes a static bash harness.
        ["/bin/bash", "-lc", script, "_guard_check", command_text],
        cwd=REPO_ROOT,
        env={**os.environ, "ELB_TERMINAL_GUARD": "0"},
        text=True,
        capture_output=True,
        check=True,
    )


def test_guard_trap_blocks_before_execution(tmp_path: Path) -> None:
    protected_home = tmp_path / "home"
    protected_home.mkdir()
    script = f"""
source {GUARD}
rm -rf "$HOME"
test -d "$HOME" && echo still-here
"""
    result = subprocess.run(  # noqa: S603 - test executes a static bash harness.
        ["/bin/bash", "-lc", script],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOME": str(protected_home),
            "ELB_TERMINAL_GUARD_TEST": "1",
        },
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == "still-here"
    assert "recursive deletion of protected paths is blocked" in result.stderr


def test_guard_allows_non_destructive_rm() -> None:
    result = _guard_check("rm scratch.txt")
    assert result.stdout.strip() == "allowed"


def test_guard_blocks_recursive_home_delete() -> None:
    result = _guard_check('rm -rf "$HOME"')
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_recursive_workspace_wipe() -> None:
    result = _guard_check("rm -rf .")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_azure_delete_operations() -> None:
    result = _guard_check("az group delete --name rg-elb --yes")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_cluster_level_kubectl_delete() -> None:
    result = _guard_check("kubectl delete namespace kube-system")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_attempts_to_disable_itself() -> None:
    result = _guard_check("trap - DEBUG")
    assert result.stdout.strip() == "blocked"


def test_guard_allows_sudo_apt_install() -> None:
    result = _guard_check("sudo apt install -y htop")
    assert result.stdout.strip() == "allowed"


def test_guard_allows_sudo_apt_get_install_multiple_packages() -> None:
    result = _guard_check("sudo apt-get install -y htop curl jq")
    assert result.stdout.strip() == "allowed"


def test_guard_allows_sudo_apt_update() -> None:
    result = _guard_check("sudo apt update")
    assert result.stdout.strip() == "allowed"


def test_guard_allows_sudo_apt_get_update() -> None:
    result = _guard_check("sudo apt-get update")
    assert result.stdout.strip() == "allowed"


def test_guard_blocks_sudo_apt_remove() -> None:
    result = _guard_check("sudo apt remove curl")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_sudo_apt_get_purge() -> None:
    result = _guard_check("sudo apt-get purge -y curl")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_sudo_apt_autoremove() -> None:
    result = _guard_check("sudo apt autoremove")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_sudo_apt_dist_upgrade() -> None:
    result = _guard_check("sudo apt-get dist-upgrade -y")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_sudo_dpkg() -> None:
    result = _guard_check("sudo dpkg -r curl")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_sudo_bash() -> None:
    result = _guard_check("sudo bash")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_sudo_rm() -> None:
    result = _guard_check("sudo rm -rf /etc")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_chained_sudo_apt_remove_after_install() -> None:
    # Even if the leading command would be allowed, a non-leading sudo
    # of a destructive subcommand must still be blocked. The DEBUG trap
    # in real shells fires once per simple command, so this is mostly a
    # safety net for one-line tests; the explicit string match catches
    # the trailing `sudo apt remove`.
    result = _guard_check("echo hi && sudo apt remove curl")
    assert result.stdout.strip() == "blocked"


def test_guard_blocks_alias_bypassing_backslash_sudo() -> None:
    # `\sudo` (backslash before sudo) suppresses alias expansion but
    # invokes the real sudo binary. The guard must still gate it so the
    # operator gets the same clear error message as for plain `sudo`.
    result = _guard_check("\\sudo rm -rf /etc")
    assert result.stdout.strip() == "blocked"


def test_guard_allows_alias_bypassing_backslash_sudo_apt_install() -> None:
    # The backslash-bypass form must still allow the whitelisted
    # subcommands; otherwise the guard would be inconsistent with the
    # plain `sudo apt install` path.
    result = _guard_check("\\sudo apt install htop")
    assert result.stdout.strip() == "allowed"


def test_guard_blocks_command_builtin_sudo() -> None:
    # `command sudo ...` bypasses shell functions named `sudo` and
    # invokes the binary directly. We deliberately treat the
    # `command sudo` form as denied (even for apt install) because the
    # only legitimate UX is plain `sudo`.
    result = _guard_check("command sudo apt install htop")
    assert result.stdout.strip() == "blocked"
