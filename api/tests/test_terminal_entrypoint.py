"""Tests for the terminal sidecar entrypoint wiring.

Responsibility: Tests for the terminal sidecar entrypoint wiring
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_ttyd_attaches_to_persistent_tmux_session`,
`test_ttyd_enables_url_arg_for_per_operator_session`,
`test_tmux_attach_wrapper_isolates_per_operator_session`,
`test_tmux_attach_wrapper_isolates_azure_credential_cache`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_entrypoint.py`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRYPOINT = REPO_ROOT / "terminal" / "entrypoint.sh"
TMUX_ATTACH = REPO_ROOT / "terminal" / "tmux-attach.sh"


def test_ttyd_attaches_to_persistent_tmux_session() -> None:
    body = ENTRYPOINT.read_text()

    assert "/usr/local/bin/ttyd" in body
    # ttyd now launches the per-operator wrapper instead of a fixed shared
    # `tmux new-session -A -s elb` so distinct operators never share a PTY.
    assert "/usr/local/bin/elb-tmux-attach" in body
    assert "new-session -A -s elb /bin/bash --login" not in body
    assert "new-session -A -D" not in body
    assert "  /bin/bash --login &" not in body


def test_ttyd_enables_url_arg_for_per_operator_session() -> None:
    body = ENTRYPOINT.read_text()

    # `-a` (--url-arg) is what lets the api proxy forward the per-operator
    # session token to the wrapper via `?arg=<token>`. Without it every
    # operator collapses back onto the wrapper's shared fallback session.
    assert "\n  -a \\" in body


def test_tmux_attach_wrapper_isolates_per_operator_session() -> None:
    body = TMUX_ATTACH.read_text()

    # The wrapper must build the session name from its first argument and use
    # `new-session -A` so the same operator re-attaches their own session.
    assert 'session="elb-${suffix}"' in body
    assert "new-session -A -s" in body
    # Defence-in-depth: the arg is sanitised to [a-z0-9] before it becomes a
    # tmux session name (argv, never shell-evaluated).
    assert "tr -cd 'a-z0-9'" in body


def test_tmux_attach_wrapper_isolates_azure_credential_cache() -> None:
    body = TMUX_ATTACH.read_text()

    # PTY isolation alone is not enough: every shell shares $HOME, so without a
    # per-operator AZURE_CONFIG_DIR one operator's `az login` token would be
    # reused by another operator's (now PTY-isolated) shell. The wrapper must
    # point each session at its own ~/.azure-<suffix> via tmux `-e`.
    assert 'azure_dir="$HOME/.azure-${suffix}"' in body
    assert "-e \"AZURE_CONFIG_DIR=$azure_dir\"" in body
    assert 'mkdir -p "$azure_dir"' in body
