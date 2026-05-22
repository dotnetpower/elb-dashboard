"""Tiny sandbox uvicorn launcher for self-upgrade end-to-end smoke testing.

Boots the FastAPI app with `AUTH_DEV_BYPASS=true` plus in-memory
backends for the upgrade-state row, build-log blob, and history blob,
so the endpoints respond without any Azure Storage credentials. The
upgrade-admin allowlist contains the dev-bypass synthetic oid. Use
this only for local smoke testing; the in-memory backends refuse to
construct in production.
"""

from __future__ import annotations

import os

# Must be set BEFORE importing api / starting uvicorn.
os.environ["AUTH_DEV_BYPASS"] = "true"
# Default to making the dev-bypass synthetic oid an admin; pass
# SMOKE_NO_ADMIN=true to test the 403 path.
if os.environ.get("SMOKE_NO_ADMIN", "").lower() != "true":
    os.environ.setdefault(
        "UPGRADE_ADMIN_OIDS", "00000000-0000-0000-0000-000000000000"
    )
else:
    os.environ.pop("UPGRADE_ADMIN_OIDS", None)
os.environ.setdefault(
    "UPGRADE_GIT_REMOTE", "https://github.com/dotnetpower/elb-dashboard.git"
)
os.environ.setdefault("PLATFORM_ACR_NAME", "sandboxacr")
os.environ.setdefault("ELB_ALLOW_INMEMORY_UPGRADE_STATE", "true")
os.environ.setdefault("ELB_ALLOW_INMEMORY_BUILD_LOGS", "true")
os.environ.setdefault("ELB_ALLOW_INMEMORY_UPGRADE_HISTORY", "true")
# Make sure tomllib parses an existing pyproject so __version__ resolves.
os.environ.pop("AZURE_TABLE_ENDPOINT", None)
os.environ.pop("AZURE_BLOB_ENDPOINT", None)

import uvicorn  # noqa: E402

from api.services.upgrade import build_logs, history, state  # noqa: E402

state.set_backend(state.InMemoryBackend())
build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
history.set_backend(history.InMemoryHistoryBackend())


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", "8087"))
    uvicorn.run("api.main:app", host="127.0.0.1", port=port, log_level="warning")
