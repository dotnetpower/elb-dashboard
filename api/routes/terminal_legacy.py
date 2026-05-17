"""Legacy `/api/terminal/{vm_name}/*` endpoints.

The Container Apps topology has no Remote Terminal VM. The browser shell is
the `terminal` sidecar, reached only via the WebSocket proxy at
`/api/terminal/ws` (see api/routes/terminal_ws.py).

These shims exist so that older SPA bundles still cached in a browser do not
crash on `404`. They return `410 Gone` with a structured body so the SPA's
error boundary can render an informative "removed" state and offer a Reload
button.

When the SPA is rebuilt to drop these calls, this module can be removed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from api.auth import CallerIdentity, require_caller

router = APIRouter(prefix="/api/terminal", tags=["terminal-legacy"])


def _gone(action: str) -> HTTPException:
    return HTTPException(
        status_code=410,
        detail={
            "code": "no_terminal_vm",
            "message": (
                f"'{action}' is not available in the bundled Container Apps "
                "topology. The browser shell is the in-process 'terminal' "
                "sidecar reached via the authenticated WebSocket at "
                "/api/terminal/ws (use POST /api/terminal/ticket first)."
            ),
        },
    )


@router.post("/provision")
def provision(_: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    raise _gone("terminal/provision")


@router.get("/status/{instance_id}")
def status(
    instance_id: str = Path(...), _: CallerIdentity = Depends(require_caller)
) -> dict[str, Any]:
    raise _gone(f"terminal/status/{instance_id}")


@router.get("/{vm_name}/password")
def password(
    vm_name: str = Path(...), _: CallerIdentity = Depends(require_caller)
) -> dict[str, Any]:
    raise _gone(f"terminal/{vm_name}/password")


@router.post("/{vm_name}/start")
def start(vm_name: str = Path(...), _: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    raise _gone(f"terminal/{vm_name}/start")


@router.post("/{vm_name}/stop")
def stop(vm_name: str = Path(...), _: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    raise _gone(f"terminal/{vm_name}/stop")


@router.post("/{vm_name}/destroy")
def destroy(
    vm_name: str = Path(...), _: CallerIdentity = Depends(require_caller)
) -> dict[str, Any]:
    raise _gone(f"terminal/{vm_name}/destroy")


@router.post("/{vm_name}/open-ssh")
def open_ssh(
    vm_name: str = Path(...),
    caller_ip: str = Query(default=""),
    _: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    raise _gone(f"terminal/{vm_name}/open-ssh")


@router.get("/{vm_name}/health")
def vm_health(
    vm_name: str = Path(...), _: CallerIdentity = Depends(require_caller)
) -> dict[str, Any]:
    raise _gone(f"terminal/{vm_name}/health")
