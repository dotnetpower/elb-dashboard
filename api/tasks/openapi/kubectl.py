"""Apply OpenAPI manifests on AKS via the terminal sidecar's kubectl.

Responsibility: Bridge the api/worker sidecars (which intentionally do not ship `az`
    or `kubectl`) to the terminal sidecar's allowlisted exec server. Fetches a one-shot
    admin kubeconfig with `az aks get-credentials`, then runs `kubectl apply -f -` or
    any other kubectl subcommand against that kubeconfig.
Edit boundaries: Shell-only work that requires the terminal sidecar binaries. Do not
    invoke `subprocess` here — go through `api.services.terminal_exec.run` so the
    exec-token + concurrency + allowlist contract is enforced.
Key entry points: `kubectl_apply`, `ensure_admin_kubeconfig`, `kubectl_run`.
Risky contracts: Calls fail with a clear RuntimeError when the terminal sidecar is
    unavailable — do not silently swallow that case (the OpenAPI deploy would otherwise
    "succeed" without applying anything). Temp kubeconfig path uses /tmp/exec (the
    sidecar's shared writable dir) and a random uuid to avoid concurrent collisions.
Validation: `uv run pytest -q api/tests/test_smoke.py api/tests/test_openapi_task.py`.
"""

from __future__ import annotations

import os
import uuid
from typing import Any


def ensure_admin_kubeconfig(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> str:
    """Fetch an admin kubeconfig in the terminal sidecar; return its path.

    Logs in with the workload MI if `az account show` reports no active
    session, then writes a fresh kubeconfig under ``/tmp/exec`` with a
    random uuid suffix so concurrent callers cannot collide. Callers are
    free to reuse the returned path across many `kubectl_run` calls in
    the same Celery task — the underlying file lives until the exec
    server's tmpdir GC sweeps it.
    """

    from api.services.terminal_exec import TerminalExecError
    from api.services.terminal_exec import run as exec_run

    # /tmp/exec is the shared writable scratch dir on the terminal sidecar's
    # exec server (configurable via EXEC_TMP_DIR). Random uuid prevents
    # collisions across concurrent deploys.
    kubeconfig_path = f"/tmp/exec/kubeconfig-{uuid.uuid4().hex}"  # noqa: S108
    account_result = exec_run(["az", "account", "show", "--only-show-errors"], timeout_seconds=30)
    if account_result.get("exit_code", 1) != 0:
        login_argv = ["az", "login", "--identity", "--allow-no-subscriptions", "--only-show-errors"]
        client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
        if client_id:
            login_argv.extend(["--client-id", client_id])
        login_result = exec_run(login_argv, timeout_seconds=120)
        if login_result.get("exit_code", 1) != 0:
            raise RuntimeError(
                "az login --identity failed in the terminal sidecar: "
                f"{(login_result.get('stderr') or login_result.get('stdout') or '').strip()[:500]}"
            )
    az_argv = [
        "az",
        "aks",
        "get-credentials",
        "--subscription",
        subscription_id,
        "--resource-group",
        resource_group,
        "--name",
        cluster_name,
        "--file",
        kubeconfig_path,
        "--overwrite-existing",
        "--admin",  # bypasses AAD interactive login from inside the sidecar
        "--only-show-errors",
    ]
    try:
        az_result = exec_run(az_argv, timeout_seconds=120)
    except TerminalExecError as exc:
        raise RuntimeError(
            "Cannot reach the terminal sidecar's exec server — the "
            "OpenAPI deploy needs `az` and `kubectl` from there. Make "
            f"sure the `terminal` sidecar is running. ({exc})"
        ) from exc
    if az_result.get("exit_code", 1) != 0:
        raise RuntimeError(
            "az aks get-credentials failed: "
            f"{(az_result.get('stderr') or az_result.get('stdout') or '').strip()[:500]}"
        )
    return kubeconfig_path


def kubectl_run(
    args: list[str],
    *,
    kubeconfig_path: str,
    stdin: str | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Run an arbitrary `kubectl ...` subcommand against `kubeconfig_path`.

    Returns the raw exec-server result dict (``exit_code``, ``stdout``,
    ``stderr``, ``duration_ms``, ``timed_out``). The caller decides how to
    treat non-zero exits — some kubectl subcommands (``wait``, ``patch``,
    ``get -o jsonpath``) are expected to fail under perfectly normal
    branches (resource not yet present, status condition not yet True),
    so this helper deliberately does NOT raise on non-zero.
    """

    from api.services.terminal_exec import run as exec_run

    argv = ["kubectl", "--kubeconfig", kubeconfig_path, *args]
    return exec_run(argv, stdin=stdin, timeout_seconds=timeout_seconds)


def kubectl_apply(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    manifest: str,
) -> str:
    """Apply a multi-doc manifest via the terminal sidecar's kubectl.

    Thin wrapper around ``ensure_admin_kubeconfig`` + ``kubectl_run`` for
    the common "fetch creds, apply YAML/JSON over stdin" flow used by the
    elb-openapi deploy. Raises on non-zero kubectl exit so the deploy
    task can short-circuit to a failed payload.
    """

    kubeconfig_path = ensure_admin_kubeconfig(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    apply_result = kubectl_run(
        ["apply", "-f", "-"],
        kubeconfig_path=kubeconfig_path,
        stdin=manifest,
        timeout_seconds=180,
    )
    if apply_result.get("exit_code", 1) != 0:
        raise RuntimeError(
            "kubectl apply failed: "
            f"{(apply_result.get('stderr') or apply_result.get('stdout') or '').strip()[:500]}"
        )
    return str(apply_result.get("stdout") or "")
