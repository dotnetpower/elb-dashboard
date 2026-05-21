"""Runtime ElasticBLAST CLI overrides for host-mode terminal exec.

Responsibility: Install local-only ElasticBLAST monkey patches without editing the sibling
elastic-blast-azure checkout.
Edit boundaries: Keep this file limited to terminal subprocess startup behavior; permanent
image patches belong in terminal/patch_elastic_blast.py.
Key entry points: `_patch_azure_submit_cleanup`.
Risky contracts: Only activate when ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP=1 so unrelated
Python processes are not changed.
Validation: `uv run pytest -q api/tests/test_terminal_runtime_overrides.py`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def _patch_azure_submit_cleanup() -> None:
    if os.environ.get("ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP") != "1":
        return
    try:
        from elastic_blast import azure_cli_glue
    except Exception:
        return

    original = azure_cli_glue.submit_command
    if getattr(original, "_elb_dashboard_fast_cleanup", False):
        return

    def submit_command(
        args: Any,
        cfg: Any,
        clean_up_stack: list[Callable[..., Any]],
        *,
        default_submit: Callable[..., int],
    ) -> int:
        return_code = original(args, cfg, clean_up_stack, default_submit=default_submit)
        if bool(getattr(args, "json", False)) and return_code == 0:
            clean_up_stack.clear()
        return return_code

    submit_command._elb_dashboard_fast_cleanup = True  # type: ignore[attr-defined]
    azure_cli_glue.submit_command = submit_command


_patch_azure_submit_cleanup()
