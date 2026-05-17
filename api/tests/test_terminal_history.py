"""Tests for the per-shell command-history capture script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HISTORY_SH = REPO_ROOT / "terminal" / "history.sh"


def _run_with_history(tmp_path: Path, commands: str) -> subprocess.CompletedProcess[str]:
    """Drive an interactive bash that sources history.sh, then runs `commands`.

    `bash -i` enables `$- == *i*`, which is how history.sh detects an
    interactive shell. We override $HOME so the script writes under
    `tmp_path` instead of polluting the real user's home.
    """
    script = f"""
set -u
source {HISTORY_SH}
{commands}
"""
    return subprocess.run(  # noqa: S603 - test executes a static bash harness.
        ["/bin/bash", "-i", "-c", script],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "USER": "elb-test-user",
            "PS1": "$ ",
            # Silence the "no job control" warning from bash -i.
            "BASH_SILENCE_DEPRECATION_WARNING": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_history_directory_created_with_restrictive_mode(tmp_path: Path) -> None:
    _run_with_history(tmp_path, "true")
    history_dir = tmp_path / ".elb-history"
    assert history_dir.is_dir(), "history directory must be created"
    assert oct(history_dir.stat().st_mode & 0o777) == "0o700"


def test_histfile_uses_per_pid_filename(tmp_path: Path) -> None:
    # PROMPT_COMMAND `history -a` only flushes when bash shows a prompt,
    # which never happens under `bash -i -c <script>`. Force a flush so
    # the per-PID file exists for inspection.
    result = _run_with_history(
        tmp_path,
        'history -s "echo from-test"\nhistory -a\necho "HISTFILE=$HISTFILE"',
    )
    history_dir = tmp_path / ".elb-history"
    files = sorted(history_dir.glob("commands.*.log"))
    assert files, (
        f"expected a per-PID HISTFILE under {history_dir}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    name = files[0].name
    assert name.startswith("commands.") and name.endswith(".log")
    assert name.removeprefix("commands.").removesuffix(".log").isdigit()
    # The HISTFILE env var must agree with the file actually created.
    assert f"HISTFILE={files[0]}" in result.stdout


def test_session_log_records_start_and_end(tmp_path: Path) -> None:
    _run_with_history(tmp_path, "true")
    sessions_log = tmp_path / ".elb-history" / "sessions.log"
    assert sessions_log.is_file(), "sessions.log must be created"
    body = sessions_log.read_text()
    assert "\tstart\n" in body, f"expected a start marker, got: {body!r}"
    assert "\tend\n" in body, f"expected an end marker, got: {body!r}"
    # Both markers must carry the same PID.
    start_pids = [line.split("\t")[1] for line in body.splitlines() if line.endswith("\tstart")]
    end_pids = [line.split("\t")[1] for line in body.splitlines() if line.endswith("\tend")]
    assert start_pids and end_pids
    assert set(start_pids) == set(end_pids)
    # The session line must record the username so audits can attribute
    # commands when multiple operators share a sidecar.
    assert "user=elb-test-user" in body


def test_prompt_command_includes_history_append(tmp_path: Path) -> None:
    result = _run_with_history(tmp_path, 'echo "PC=[$PROMPT_COMMAND]"')
    assert "PC=[" in result.stdout
    # `history -a` must be wired into PROMPT_COMMAND so commands flush to
    # disk on every Enter press, not only at shell exit.
    assert "history -a" in result.stdout


def test_history_disabled_for_non_interactive_shell(tmp_path: Path) -> None:
    # Same script but bash -c (non-interactive) — must NOT create files.
    subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f"source {HISTORY_SH}"],
        cwd=REPO_ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert not (tmp_path / ".elb-history").exists()


def test_sessions_log_is_owner_only(tmp_path: Path) -> None:
    # The audit ledger may sit on a shared Azure Files mount visible to
    # other tools — keep it owner-only (0600) regardless of the
    # inherited umask, mirroring bash's HISTFILE behaviour.
    _run_with_history(tmp_path, "true")
    sessions_log = tmp_path / ".elb-history" / "sessions.log"
    assert sessions_log.is_file()
    assert oct(sessions_log.stat().st_mode & 0o777) == "0o600"


def test_resourcing_does_not_duplicate_start_record(tmp_path: Path) -> None:
    # Sourcing the script a second time inside the same shell (e.g. a
    # nested `bash -l` or a re-entered profile.d loader) must NOT write
    # a second start record for the same PID. The ELB_HISTORY_SESSION_LOGGED
    # latch guards this.
    _run_with_history(
        tmp_path,
        f"source {HISTORY_SH}\nsource {HISTORY_SH}\ntrue",
    )
    sessions_log = tmp_path / ".elb-history" / "sessions.log"
    body = sessions_log.read_text()
    starts = [ln for ln in body.splitlines() if ln.endswith("\tstart")]
    assert len(starts) == 1, f"expected exactly one start record, got: {body!r}"
