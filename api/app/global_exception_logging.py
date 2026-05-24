"""Process-wide exception logging hooks for the API sidecar.

Responsibility: Install idempotent sys/threading/asyncio exception hooks so
background crashes outside the request middleware still land in structured logs.
Edit boundaries: Do not add request handling or Azure SDK calls here; keep this
module limited to process-level logging hooks.
Key entry points: `install_global_exception_hooks`, `install_asyncio_exception_handler`.
Risky contracts: Hooks must chain to the original interpreter handlers where
possible and must never raise from the hook itself.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from types import TracebackType
from typing import Any

LOGGER = logging.getLogger("api.app.global_exception")

_HOOKS_INSTALLED = False
_ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREADING_EXCEPTHOOK = getattr(threading, "excepthook", None)


def _safe_log_exception(
    label: str,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    exc_traceback: TracebackType | None,
    **extra: Any,
) -> None:
    try:
        if exc_type is KeyboardInterrupt:
            return
        LOGGER.critical(
            "unhandled_%s exc=%s extra=%s",
            label,
            getattr(exc_type, "__name__", str(exc_type)),
            extra,
            exc_info=(exc_type, exc_value, exc_traceback) if exc_type and exc_value else None,
        )
    except Exception as hook_exc:
        # Last-resort hooks must never make interpreter shutdown worse.
        try:
            print(
                f"global exception logger failed while handling {label}: {type(hook_exc).__name__}",
                file=sys.stderr,
            )
        except Exception:
            return


def install_global_exception_hooks() -> None:
    """Install process-wide exception hooks once per interpreter."""
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return
    _HOOKS_INSTALLED = True

    def _sys_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        _safe_log_exception("sys", exc_type, exc_value, exc_traceback)
        try:
            _ORIGINAL_SYS_EXCEPTHOOK(exc_type, exc_value, exc_traceback)
        except Exception as hook_exc:
            print(
                f"original sys.excepthook failed: {type(hook_exc).__name__}",
                file=sys.stderr,
            )

    sys.excepthook = _sys_hook

    if _ORIGINAL_THREADING_EXCEPTHOOK is not None:

        def _thread_hook(args: threading.ExceptHookArgs) -> None:
            _safe_log_exception(
                "thread",
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                thread=getattr(args.thread, "name", None),
            )
            try:
                _ORIGINAL_THREADING_EXCEPTHOOK(args)
            except Exception as hook_exc:
                print(
                    f"original threading.excepthook failed: {type(hook_exc).__name__}",
                    file=sys.stderr,
                )

        threading.excepthook = _thread_hook


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Install a loop-level exception handler that chains to the previous one."""
    previous = loop.get_exception_handler()

    def _asyncio_hook(loop_in: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            _safe_log_exception(
                "asyncio",
                type(exc),
                exc,
                exc.__traceback__,
                message=context.get("message"),
                future=repr(context.get("future"))[:300]
                if context.get("future") is not None
                else None,
            )
        else:
            try:
                LOGGER.error(
                    "unhandled_asyncio message=%s context=%s",
                    context.get("message"),
                    context,
                )
            except Exception as hook_exc:
                print(
                    f"asyncio exception logger failed: {type(hook_exc).__name__}",
                    file=sys.stderr,
                )
        if previous is not None:
            previous(loop_in, context)
        else:
            loop_in.default_exception_handler(context)

    loop.set_exception_handler(_asyncio_hook)
