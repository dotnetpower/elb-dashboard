"""Persona matrix regression test — guards the four caller personas required
by .github/copilot-instructions.md §12a Rule 2.

Module summary: A future hardening PR can accidentally promote a route to a
stricter gate (e.g. swap `require_caller` for `require_upgrade_admin`) and
silently break a Reader-only operator who was relying on the dashboard's read
paths. This test asserts that, for each persona, the auth-layer contracts the
charter promises hold:

    * owner_caller          (subscription Owner with `UpgradeAdmin` app role)
    * contributor_caller    (RG Contributor + Blob Data Contributor)
    * reader_caller         (subscription Reader + Blob Data Reader)
    * dev_bypass_caller     (`AUTH_DEV_BYPASS=true`, OID 0000…0)

Responsibility: Auth-gate contract test only — does not exercise the actual
    Azure RBAC layer (that is the Capability Probe's job).
Edit boundaries: New personas must be added to the charter §12a Rule 2 table
    in the same PR. The Reader allowlist lives in
    `api/tests/persona_reader_allowlist.py` and changes there require a
    separate maintainer-reviewed PR per the charter.
Key entry points: `owner_caller`, `contributor_caller`, `reader_caller`,
    `dev_bypass_caller`, `test_*`.
Risky contracts: The Reader allowlist references handler functions by
    dotted-path import. If a route is renamed, the import breaks and this
    test fails loudly — that is the intended behaviour, the rename PR must
    update the allowlist in lockstep.
Validation: `uv run pytest -q api/tests/test_persona_matrix.py`.
"""

from __future__ import annotations

import importlib

import pytest
from api.auth import DEV_BYPASS_OID, CallerIdentity, is_dev_bypass_caller
from api.services.upgrade.auth import (
    UPGRADE_ADMIN_OIDS_ENV,
    UPGRADE_ADMIN_ROLE,
    is_upgrade_admin,
    require_upgrade_admin,
)
from api.tests.persona_reader_allowlist import READER_ALLOWLIST, ReaderAllowedRoute
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

# ---------------------------------------------------------------------------
# Persona fixtures.
#
# Personas are synthetic `CallerIdentity` values. They mirror what a real
# MSAL bearer token would have looked like after `require_caller` validated
# it. None of these are real Azure principals.
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner_caller() -> CallerIdentity:
    """Subscription Owner with the `UpgradeAdmin` MSAL app role asserted."""
    return CallerIdentity(
        object_id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        upn="owner@example.test",
        raw_token="synthetic-owner",
        claims={"roles": [UPGRADE_ADMIN_ROLE]},
    )


@pytest.fixture()
def contributor_caller() -> CallerIdentity:
    """RG Contributor + Blob Data Contributor — no UpgradeAdmin role."""
    return CallerIdentity(
        object_id="33333333-3333-3333-3333-333333333333",
        tenant_id="22222222-2222-2222-2222-222222222222",
        upn="contributor@example.test",
        raw_token="synthetic-contributor",
        claims={"roles": []},
    )


@pytest.fixture()
def reader_caller() -> CallerIdentity:
    """Subscription Reader + Blob Data Reader — must NOT carry UpgradeAdmin."""
    return CallerIdentity(
        object_id="44444444-4444-4444-4444-444444444444",
        tenant_id="22222222-2222-2222-2222-222222222222",
        upn="reader@example.test",
        raw_token="synthetic-reader",
        claims={"roles": []},
    )


@pytest.fixture()
def dev_bypass_caller() -> CallerIdentity:
    """Synthetic identity returned by `AUTH_DEV_BYPASS=true`."""
    return CallerIdentity(
        object_id=DEV_BYPASS_OID,
        tenant_id="dev-bypass",
        upn="dev-bypass@local",
        raw_token="",
        claims={"dev_bypass": True},
    )


# ---------------------------------------------------------------------------
# 1. is_dev_bypass_caller() — synthetic-identity detection.
# ---------------------------------------------------------------------------


def test_dev_bypass_caller_is_recognised_locally(
    monkeypatch: pytest.MonkeyPatch, dev_bypass_caller: CallerIdentity
) -> None:
    """Local dev (no `CONTAINER_APP_NAME` env) recognises the synthetic OID."""
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    assert is_dev_bypass_caller(dev_bypass_caller) is True


def test_dev_bypass_caller_is_refused_in_container_app(
    monkeypatch: pytest.MonkeyPatch, dev_bypass_caller: CallerIdentity
) -> None:
    """Deployed Container App MUST refuse to honour the dev-bypass OID.

    Defence in depth: a stale `AUTH_DEV_BYPASS=true` in a cloud revision
    cannot turn into a privilege-escalation primitive when
    `CONTAINER_APP_NAME` is set by the ACA platform.
    """
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    assert is_dev_bypass_caller(dev_bypass_caller) is False


@pytest.mark.parametrize("persona_name", ["owner_caller", "contributor_caller", "reader_caller"])
def test_real_personas_are_not_dev_bypass(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, persona_name: str
) -> None:
    """Owner / Contributor / Reader must never be mistaken for dev bypass.

    Guards against a future change that broadens `is_dev_bypass_caller` to
    match on something other than the literal sentinel OID.
    """
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    persona: CallerIdentity = request.getfixturevalue(persona_name)
    assert is_dev_bypass_caller(persona) is False


# ---------------------------------------------------------------------------
# 2. is_upgrade_admin() — admin escalation surface.
# ---------------------------------------------------------------------------


def test_owner_with_upgrade_role_is_upgrade_admin(
    monkeypatch: pytest.MonkeyPatch, owner_caller: CallerIdentity
) -> None:
    monkeypatch.delenv(UPGRADE_ADMIN_OIDS_ENV, raising=False)
    assert is_upgrade_admin(owner_caller) is True


def test_contributor_is_not_upgrade_admin(
    monkeypatch: pytest.MonkeyPatch, contributor_caller: CallerIdentity
) -> None:
    monkeypatch.delenv(UPGRADE_ADMIN_OIDS_ENV, raising=False)
    assert is_upgrade_admin(contributor_caller) is False


def test_reader_is_not_upgrade_admin(
    monkeypatch: pytest.MonkeyPatch, reader_caller: CallerIdentity
) -> None:
    monkeypatch.delenv(UPGRADE_ADMIN_OIDS_ENV, raising=False)
    assert is_upgrade_admin(reader_caller) is False


def test_oid_allowlist_promotes_caller(
    monkeypatch: pytest.MonkeyPatch, contributor_caller: CallerIdentity
) -> None:
    """`UPGRADE_ADMIN_OIDS` allowlist still works as documented."""
    monkeypatch.setenv(UPGRADE_ADMIN_OIDS_ENV, contributor_caller.object_id)
    assert is_upgrade_admin(contributor_caller) is True


# ---------------------------------------------------------------------------
# 3. Reader allowlist — every handler must keep a non-admin gate.
#
# The risk this guards against: a hardening PR adds `Depends(
# require_upgrade_admin)` (or any stricter future gate) to a Reader-required
# route and silently breaks the dashboard for a subscription Reader.
#
# Approach: walk the FastAPI app's APIRoute tree, find the entry's handler
# function, and assert `require_upgrade_admin` does not appear in the
# flattened dependency tree.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_under_test() -> FastAPI:
    """Build the FastAPI app once for the module so we can inspect routes."""
    import os

    # AUTH_DEV_BYPASS keeps app boot side-effect-free in this test process.
    os.environ.setdefault("AUTH_DEV_BYPASS", "true")
    os.environ.setdefault("AZURE_TENANT_ID", "common")
    os.environ.setdefault("API_CLIENT_ID", "00000000-0000-0000-0000-000000000001")

    from api.main import create_app

    return create_app()


def _find_route_for_handler(app: FastAPI, handler: object) -> APIRoute | None:
    """Return the APIRoute whose endpoint is the given handler function."""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.endpoint is handler:
            return route
    return None


def _walk_dependants(dependant: Dependant) -> list[Dependant]:
    """Yield ``dependant`` and every nested ``Depends()`` sub-dependant.

    FastAPI's ``get_flat_dependant`` flattens path/query/header/body params
    but does NOT flatten the ``.dependencies`` chain itself, so we have to
    walk it manually to discover transitive ``Depends(require_upgrade_admin)``
    usage (e.g. a route depending on a wrapper that in turn depends on the
    admin gate).
    """
    stack: list[Dependant] = [dependant]
    seen: list[Dependant] = []
    while stack:
        node = stack.pop()
        seen.append(node)
        stack.extend(node.dependencies)
    return seen


def _route_depends_on_upgrade_admin(route: APIRoute) -> bool:
    """True iff any (transitive) Depends() in ``route`` resolves to
    ``require_upgrade_admin``."""
    for node in _walk_dependants(route.dependant):
        if node.call is require_upgrade_admin:
            return True
    return False


@pytest.mark.parametrize(
    "entry",
    READER_ALLOWLIST,
    ids=lambda entry: f"{entry.module}::{entry.function}",
)
def test_reader_allowlist_handler_is_importable(entry: ReaderAllowedRoute) -> None:
    """Each entry must reference a real, importable handler function.

    If this fails, the route was renamed or removed. Split the rename / remove
    into its own PR that also updates `persona_reader_allowlist.py`.
    """
    module = importlib.import_module(entry.module)
    handler = getattr(module, entry.function, None)
    assert handler is not None, (
        f"Reader-allowlisted handler {entry.module}::{entry.function} no longer exists. "
        f"Reason it was allowlisted: {entry.why}. "
        f"Update api/tests/persona_reader_allowlist.py in a separate PR per §12a Rule 2."
    )


@pytest.mark.parametrize(
    "entry",
    READER_ALLOWLIST,
    ids=lambda entry: f"{entry.module}::{entry.function}",
)
def test_reader_allowlist_route_does_not_require_upgrade_admin(
    app_under_test: FastAPI, entry: ReaderAllowedRoute
) -> None:
    """Reader-required routes must NOT depend on `require_upgrade_admin`.

    If this fails, a hardening PR has promoted a Reader-required route to
    an admin-only gate. Either revert that promotion or split out a separate
    PR that removes the entry from the Reader allowlist (per §12a Rule 2).
    """
    module = importlib.import_module(entry.module)
    handler = getattr(module, entry.function)
    route = _find_route_for_handler(app_under_test, handler)
    assert route is not None, (
        f"Handler {entry.module}::{entry.function} is not registered on the "
        f"FastAPI app — was the router include skipped?"
    )
    assert not _route_depends_on_upgrade_admin(route), (
        f"Reader-required handler {entry.module}::{entry.function} now depends "
        f"on require_upgrade_admin. Reason it was allowlisted: {entry.why}. "
        f"Either drop that dependency or open a separate PR removing the entry "
        f"from api/tests/persona_reader_allowlist.py (per §12a Rule 2)."
    )


# ---------------------------------------------------------------------------
# 4. Admin routes must keep their gate — sanity check that the matrix is
#    actually capable of distinguishing read-only vs admin routes.
# ---------------------------------------------------------------------------


def test_known_admin_route_still_requires_upgrade_admin(
    app_under_test: FastAPI,
) -> None:
    """At least one upgrade-mutating route must still gate on the admin role.

    If this assertion fails, the admin gate has been removed wholesale —
    every Reader allowlist test above also becomes vacuous because there is
    no longer any distinction between the personas. Treat it as a load-bearing
    canary for the persona-matrix machinery itself.
    """
    from api.routes import upgrade as upgrade_route

    # Walk the upgrade router and confirm at least one route still uses the
    # admin gate. We do not pin a specific endpoint name so a refactor of the
    # upgrade routes does not break this canary.
    saw_admin_gate = False
    for route in upgrade_route.router.routes:
        if isinstance(route, APIRoute) and _route_depends_on_upgrade_admin(route):
            saw_admin_gate = True
            break
    assert saw_admin_gate, (
        "No route in api.routes.upgrade depends on require_upgrade_admin — "
        "either the admin gate was removed wholesale (which would invalidate "
        "the persona matrix) or the upgrade router was renamed. Investigate "
        "before merging."
    )


# ---------------------------------------------------------------------------
# 5. PR-3 fail-closed production guards (audit P0 #4 #5, P1 #11).
#
# These tests assert the three closed escape hatches stay closed:
#   * TERMINAL_WS_ALLOW_ANY_ORIGIN cannot be true inside a deployed
#     Container App revision (CSWSH defence).
#   * terminal/exec_server.py refuses to start with a non-loopback
#     EXEC_HOST inside a Container App.
#   * is_upgrade_admin() rejects the DEV_BYPASS_OID when CONTAINER_APP_NAME
#     is set, even if UPGRADE_ADMIN_OIDS accidentally lists it.
# ---------------------------------------------------------------------------


def test_terminal_ws_allow_any_origin_force_disabled_in_container_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TERMINAL_WS_ALLOW_ANY_ORIGIN=true` must NOT take effect when the
    process is running inside a Container Apps revision.

    Re-imports `api.routes.terminal.ws` under the deployed-env profile so the
    module-level toggle resolves with the platform marker set. The same
    re-import with the marker cleared must restore the local-dev behaviour.
    """
    import importlib

    monkeypatch.setenv("TERMINAL_WS_ALLOW_ANY_ORIGIN", "true")
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    import api.routes.terminal.ws as ws_mod

    ws_mod = importlib.reload(ws_mod)
    try:
        assert ws_mod._TERMINAL_WS_ALLOW_ANY_ORIGIN is False, (
            "Audit P0 #4 regression: TERMINAL_WS_ALLOW_ANY_ORIGIN took effect "
            "inside a Container Apps revision (CSWSH escape hatch re-opened)."
        )
    finally:
        # Reset to local-dev profile so other tests see the documented behaviour.
        monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
        importlib.reload(ws_mod)


def test_terminal_ws_allow_any_origin_works_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass continues to work for local dev (no `CONTAINER_APP_NAME`)."""
    import importlib

    monkeypatch.setenv("TERMINAL_WS_ALLOW_ANY_ORIGIN", "true")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    import api.routes.terminal.ws as ws_mod

    ws_mod = importlib.reload(ws_mod)
    try:
        assert ws_mod._TERMINAL_WS_ALLOW_ANY_ORIGIN is True
    finally:
        # Reset the env var so other tests do not inherit the override.
        monkeypatch.delenv("TERMINAL_WS_ALLOW_ANY_ORIGIN", raising=False)
        importlib.reload(ws_mod)


@pytest.mark.parametrize("bad_host", ["0.0.0.0", "192.168.1.5", "10.0.0.1", "::"])
def test_exec_server_refuses_non_loopback_bind_in_container_app(
    monkeypatch: pytest.MonkeyPatch, bad_host: str
) -> None:
    """exec_server.py must hard-fail at import when `EXEC_HOST` is anything
    other than loopback inside a Container Apps revision.

    Re-imports the module so the top-level guard runs under the deployed-env
    profile. Failure mode is a `RuntimeError` raised at import time — that is
    the contract: a misconfigured deploy is visible immediately rather than
    after the first malicious request reaches the pod IP.
    """
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setenv("EXEC_HOST", bad_host)
    # Forget any cached copy so the module-level guard runs again.
    sys.modules.pop("exec_server", None)
    exec_server_path = (
        Path(__file__).resolve().parent.parent.parent / "terminal" / "exec_server.py"
    )
    spec = importlib.util.spec_from_file_location("exec_server", exec_server_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        with pytest.raises(RuntimeError, match="refuses to start"):
            spec.loader.exec_module(module)
    finally:
        sys.modules.pop("exec_server", None)
        monkeypatch.delenv("EXEC_HOST", raising=False)
        monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)


def test_exec_server_allows_loopback_bind_in_container_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legitimate production binding (loopback) still imports cleanly."""
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setenv("EXEC_HOST", "127.0.0.1")
    sys.modules.pop("exec_server", None)
    exec_server_path = (
        Path(__file__).resolve().parent.parent.parent / "terminal" / "exec_server.py"
    )
    spec = importlib.util.spec_from_file_location("exec_server", exec_server_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        assert module.LISTEN_HOST == "127.0.0.1"
    finally:
        sys.modules.pop("exec_server", None)
        monkeypatch.delenv("EXEC_HOST", raising=False)
        monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)


def test_upgrade_admin_rejects_dev_bypass_in_container_app(
    monkeypatch: pytest.MonkeyPatch, dev_bypass_caller: CallerIdentity
) -> None:
    """Even if the dev-bypass OID accidentally lands in `UPGRADE_ADMIN_OIDS`,
    the deployed Container App must refuse to recognise it as admin.

    Defence in depth so a stale `AUTH_DEV_BYPASS=true` cloud revision cannot
    escalate into a Container App image swap.
    """
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setenv(UPGRADE_ADMIN_OIDS_ENV, DEV_BYPASS_OID)
    assert is_upgrade_admin(dev_bypass_caller) is False, (
        "Audit P1 #11 regression: dev-bypass OID was accepted as upgrade admin "
        "inside a Container Apps revision."
    )


def test_upgrade_admin_still_honours_dev_bypass_locally(
    monkeypatch: pytest.MonkeyPatch, dev_bypass_caller: CallerIdentity
) -> None:
    """Local dev (no `CONTAINER_APP_NAME`) keeps the documented escape hatch
    so `UPGRADE_ADMIN_OIDS=<dev-bypass-oid>` continues to let tests exercise
    upgrade routes."""
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv(UPGRADE_ADMIN_OIDS_ENV, DEV_BYPASS_OID)
    assert is_upgrade_admin(dev_bypass_caller) is True
