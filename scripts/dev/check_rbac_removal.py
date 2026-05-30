#!/usr/bin/env python3
"""Pre-deploy guard that halts when an `azd provision` would DELETE an
existing `Microsoft.Authorization/roleAssignments` resource.

Responsibility: Parse the output of `az deployment sub what-if --no-pretty-print
    --output json` (or read an equivalent JSON document from disk / stdin)
    and refuse to let the deployment proceed if any role assignment is
    scheduled for deletion. Implements charter §12a Rule 7 — the
    machine-checked complement to the human-discipline 2-phase ADD-then-REMOVE
    pattern in Rule 1.
Edit boundaries: Pure read-only. Does NOT mutate any Azure resource. Does
    NOT decide which roles are "important" — it flags every roleAssignment
    deletion and lets the operator acknowledge via `ACCEPT_RBAC_REMOVAL`.
Key entry points: `find_rbac_removals`, `summarise_change`, `main`.
Risky contracts: The env-var gate `STRICT_RBAC_REMOVAL_HALT` defaults to
    OFF per charter §12a Rule 4. When unset the script logs findings and
    returns 0 (warn-only). When set to a truthy value the script returns
    a non-zero exit code on any unaccepted removal, which causes
    `azd provision`'s preprovision hook to abort before ARM is touched.
    `ACCEPT_RBAC_REMOVAL` must reference a PR (e.g.
    `phase-2-of-pr-42` or `phase-2 of 2 (see PR-42)`) so the override is
    auditable in shell history and git log.
Validation: `uv run pytest -q api/tests/test_check_rbac_removal.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from typing import Any

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_BAD_ENV = 2
EXIT_HALT = 3
EXIT_AZ_FAILED = 4

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROLE_ASSIGNMENT_TYPE = "/providers/microsoft.authorization/roleassignments/"
ROLE_DEFINITION_TYPE = "/providers/microsoft.authorization/roledefinitions/"
REMOVAL_CHANGE_TYPES = frozenset({"delete", "deploymentmode"})

ACCEPT_PATTERN = re.compile(
    r"phase[-\s]?2(?:[-\s](?:of|to)[-\s]?\d+)?[-\s](?:of[-\s])?(?:pr[-#]?\d+|"
    r"\(?see[-\s]+(?:pr[-#]?\d+|#\d+)\)?|#\d+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _emit(prefix: str, message: str) -> None:
    print(f"{prefix} {message}", flush=True)


def info(message: str) -> None:
    _emit("[rbac-guard]", message)


def warn(message: str) -> None:
    _emit("[rbac-guard] WARN:", message)


def err(message: str) -> None:
    _emit("[rbac-guard] ERROR:", message)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _normalise_resource_id(value: Any) -> str:
    return str(value or "").lower()


def find_rbac_removals(whatif: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the subset of `whatif["changes"]` that delete roleAssignments.

    Accepts both the bare CLI shape (`{"changes": [...]}`) and the wrapped
    deployment-resource shape (`{"properties": {"changes": [...]}}`).
    """
    if not isinstance(whatif, dict):
        return []
    changes = whatif.get("changes")
    if changes is None:
        properties = whatif.get("properties") or {}
        if isinstance(properties, dict):
            changes = properties.get("changes")
    if not isinstance(changes, list):
        return []
    out: list[dict[str, Any]] = []
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        change_type = str(ch.get("changeType") or "").lower()
        if change_type not in REMOVAL_CHANGE_TYPES:
            continue
        rid = _normalise_resource_id(ch.get("resourceId"))
        if ROLE_ASSIGNMENT_TYPE not in rid:
            continue
        out.append(ch)
    return out


def summarise_change(change: dict[str, Any]) -> str:
    """Human-readable one-line description of a role-assignment deletion."""
    rid = str(change.get("resourceId") or "<unknown>")
    before = change.get("before") or {}
    props = (
        before.get("properties") if isinstance(before, dict) else None
    ) or {}
    principal_id = props.get("principalId") or "<unknown-principal>"
    principal_type = props.get("principalType") or "<unknown-type>"
    role_def = str(props.get("roleDefinitionId") or "")
    role_guid = role_def.rsplit("/", 1)[-1] if role_def else "<unknown-role>"
    scope = props.get("scope") or _scope_from_role_assignment_id(rid)
    return (
        f"  - principal={principal_id} ({principal_type}) "
        f"role={role_guid} scope={scope} resourceId={rid}"
    )


def _scope_from_role_assignment_id(rid: str) -> str:
    """Return the scope portion of a roleAssignment resource id.

    The format is `{scope}/providers/Microsoft.Authorization/roleAssignments/{guid}`,
    so the scope is everything before the type segment.
    """
    idx = rid.lower().find(ROLE_ASSIGNMENT_TYPE)
    if idx < 0:
        return "<unknown-scope>"
    return rid[:idx] or "<root>"


# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------
def is_strict_enabled(env: dict[str, str]) -> bool:
    raw = (env.get("STRICT_RBAC_REMOVAL_HALT") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def acceptance_token(env: dict[str, str]) -> str:
    return (env.get("ACCEPT_RBAC_REMOVAL") or "").strip()


def is_acceptance_valid(token: str) -> bool:
    if not token:
        return False
    return bool(ACCEPT_PATTERN.search(token))


# ---------------------------------------------------------------------------
# What-if source
# ---------------------------------------------------------------------------
def load_whatif(source: str) -> dict[str, Any]:
    if source == "-":
        return json.loads(sys.stdin.read())
    with open(source, encoding="utf-8") as fh:
        return json.load(fh)


def compute_whatif(
    *,
    subscription: str,
    location: str,
    template_file: str,
    parameters: list[str],
    extra_args: Iterable[str] = (),
) -> dict[str, Any]:
    """Invoke `az deployment sub what-if` and return the parsed JSON."""
    cmd = [
        "az",
        "deployment",
        "sub",
        "what-if",
        "--subscription",
        subscription,
        "--location",
        location,
        "--template-file",
        template_file,
        "--no-pretty-print",
        "--output",
        "json",
    ]
    for kv in parameters:
        cmd.extend(["--parameters", kv])
    cmd.extend(extra_args)
    info(f"az deployment sub what-if (template={template_file}, location={location})")
    # S603: cmd is constructed from validated CLI flags only; no shell.
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        err(
            "az deployment sub what-if failed "
            f"(exit={proc.returncode}); stderr tail: {proc.stderr.strip()[-500:]}"
        )
        raise SystemExit(EXIT_AZ_FAILED)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        err(f"failed to parse what-if JSON: {exc}; stdout tail: {proc.stdout[-500:]}")
        raise SystemExit(EXIT_AZ_FAILED) from exc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Halt azd provision when a Bicep change would DELETE a "
            "Microsoft.Authorization/roleAssignments resource. Enabled by "
            "STRICT_RBAC_REMOVAL_HALT=true; acknowledge intended removals "
            "with ACCEPT_RBAC_REMOVAL=phase-2-of-pr-NN."
        )
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--from-json",
        metavar="FILE",
        help="Path to a what-if JSON document (or '-' to read from stdin).",
    )
    src.add_argument(
        "--compute",
        action="store_true",
        help="Invoke 'az deployment sub what-if' to compute the document.",
    )
    parser.add_argument("--subscription", help="Required with --compute.")
    parser.add_argument("--location", help="Required with --compute.")
    parser.add_argument(
        "--template-file",
        default="infra/main.bicep",
        help="Bicep template to evaluate (default: infra/main.bicep).",
    )
    parser.add_argument(
        "--parameter",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Bicep parameter override; repeat for multiple values.",
    )
    return parser


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env = dict(os.environ) if env is None else dict(env)

    try:
        if args.compute:
            if not args.subscription or not args.location:
                err("--compute requires --subscription and --location")
                return EXIT_BAD_ENV
            whatif = compute_whatif(
                subscription=args.subscription,
                location=args.location,
                template_file=args.template_file,
                parameters=args.parameter,
            )
        else:
            whatif = load_whatif(args.from_json)
    except SystemExit as exc:
        return int(exc.code or 0) or EXIT_AZ_FAILED

    removals = find_rbac_removals(whatif)
    if not removals:
        info("no roleAssignment deletions detected")
        return EXIT_OK

    info(f"detected {len(removals)} roleAssignment deletion(s) in what-if:")
    for change in removals:
        print(summarise_change(change), flush=True)

    strict = is_strict_enabled(env)
    accept = acceptance_token(env)

    if not strict:
        warn(
            "STRICT_RBAC_REMOVAL_HALT is OFF — proceeding without halt. "
            "Charter §12a Rule 4 transition will flip this default once "
            "the soak window completes."
        )
        return EXIT_OK

    if accept and is_acceptance_valid(accept):
        info(
            "ACCEPT_RBAC_REMOVAL satisfied "
            f"(token={accept!r}); proceeding with deployment."
        )
        return EXIT_OK

    if accept:
        err(
            "ACCEPT_RBAC_REMOVAL is set but does not match the required "
            "pattern. Use a value like 'phase-2-of-pr-42' or "
            "'phase-2 of 2 (see PR-42)'. Refusing to deploy."
        )
    else:
        err(
            "STRICT_RBAC_REMOVAL_HALT is ON and ACCEPT_RBAC_REMOVAL is not "
            "set. Refusing to deploy. If the removal is intentional, set "
            "ACCEPT_RBAC_REMOVAL='phase-2-of-pr-<N>' (e.g. phase-2-of-pr-42)."
        )
    err(
        "See charter .github/copilot-instructions.md §12a Rule 1 + Rule 7 for "
        "the 2-phase ADD-then-REMOVE workflow."
    )
    return EXIT_HALT


if __name__ == "__main__":
    sys.exit(main())
