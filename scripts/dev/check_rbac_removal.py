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
Key entry points: `find_rbac_removals`, `summarise_change`, `main`,
    `is_strict_enabled`, `is_acceptance_valid`, `compute_whatif`,
    `load_whatif`, `role_name_for_guid`.
Risky contracts: The env-var gate `STRICT_RBAC_REMOVAL_HALT` defaults to
    OFF per charter §12a Rule 4. When unset the script logs findings and
    returns 0 (warn-only). When set to a truthy value the script returns
    a non-zero exit code on any unaccepted removal, which causes
    `azd provision`'s preprovision hook to abort before ARM is touched.
    `ACCEPT_RBAC_REMOVAL` must reference a PR (e.g.
    `phase-2-of-pr-42` or `phase-2 of 2 (see PR-42)`) so the override is
    auditable in shell history and git log.

    Exit codes:
        0  OK / warn-only / acknowledged removal
        2  bad CLI usage (mutually-exclusive group, missing --subscription,
           missing --template-file, non-existent --from-json file)
        3  HALT — STRICT_RBAC_REMOVAL_HALT=true and unaccepted removal(s)
        4  az invocation failed OR malformed JSON document
Validation: `uv run pytest -q api/tests/test_check_rbac_removal.py`.
    Local end-to-end check (no halt — warn-only):
        bash scripts/dev/preflight_rbac_removal.sh
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

# Built-in Azure RBAC role GUIDs for which a removal is the most likely to
# cause a production outage. The list is intentionally focused on roles that
# `infra/modules/*.bicep` actually grants today plus the 3 broadest built-in
# roles (Owner / Contributor / Reader) so a removal log line carries an
# immediately recognisable name without the operator having to look up
# `az role definition show --name <guid>`.
# Source: https://learn.microsoft.com/azure/role-based-access-control/built-in-roles
_BUILTIN_ROLE_GUIDS: dict[str, str] = {
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635": "Owner",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "acdd72a7-3385-48ef-bd42-f606fba81ae7": "Reader",
    "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9": "User Access Administrator",
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe": "Storage Blob Data Contributor",
    "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1": "Storage Blob Data Reader",
    "974c5e8b-45b9-4653-ba55-5f855dd0fb88": "Storage Queue Data Contributor",
    "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3": "Storage Table Data Contributor",
    "7f951dda-4ed3-4680-a7ca-43fe172d538d": "AcrPull",
    "8311e382-0749-4cb8-b61a-304f252e45ec": "AcrPush",
    "b86a8fe4-44ce-4948-aee5-eccb2c155cd7": "Key Vault Secrets Officer",
    "4633458b-17de-408a-b874-0445c86b69e6": "Key Vault Secrets User",
    "00482a5a-887f-4fb3-b363-3b7fe8e74483": "Key Vault Administrator",
    "f25e0fa2-a7c8-4377-a976-54943a77a395": "Key Vault Contributor",
    "21090545-7ca7-4776-b22c-e363652d74d2": "Key Vault Reader",
    "73c42c96-874c-492b-b04d-ab87d138a893": "Log Analytics Reader",
    "92aaf0da-9dab-42b6-94a3-d43ce8d16293": "Log Analytics Contributor",
    "f5819b54-e033-4d82-ac1d-d5a2c0aedbef": "Azure Container Apps Operator",
}

# `ACCEPT_RBAC_REMOVAL` tokens — must explicitly reference a phase-2 PR so
# the override is searchable in shell history and git log. Examples accepted:
#     phase-2-of-pr-42
#     phase-2 of pr-42
#     phase-2 of 2 (see PR-42)
#     phase-2 of 2 (see #42)
ACCEPT_PATTERN = re.compile(
    r"phase[-\s]?2(?:[-\s](?:of|to)[-\s]?\d+)?[-\s](?:of[-\s])?(?:pr[-#]?\d+|"
    r"\(?see[-\s]+(?:pr[-#]?\d+|#\d+)\)?|#\d+)",
    re.IGNORECASE,
)

# Recognised truthy values for `STRICT_RBAC_REMOVAL_HALT`. Includes the
# values most commonly used by other tooling in the repo + the Azure-style
# `enabled` so the gate behaves predictably regardless of the operator's
# convention.
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


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


def _unwrap_changes(whatif: dict[str, Any]) -> list[Any]:
    """Return the `changes` array regardless of envelope nesting depth.

    Azure surfaces three documented shapes for what-if responses:

    1. CLI direct output: `{"changes": [...]}`
    2. ARM deployment resource: `{"properties": {"changes": [...]}}`
    3. Doubly-wrapped (some SDK / REST responses):
       `{"properties": {"properties": {"changes": [...]}}}`

    Walk up to two `properties` levels before giving up so future SDK
    refactors do not silently break the guard.
    """
    node: Any = whatif
    for _ in range(3):
        if not isinstance(node, dict):
            return []
        changes = node.get("changes")
        if isinstance(changes, list):
            return changes
        node = node.get("properties")
        if node is None:
            return []
    return []


def find_rbac_removals(whatif: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the subset of `whatif["changes"]` that delete roleAssignments.

    Accepts the bare CLI shape (`{"changes": [...]}`), the wrapped
    deployment-resource shape (`{"properties": {"changes": [...]}}`),
    and the doubly-wrapped SDK shape.
    """
    if not isinstance(whatif, dict):
        return []
    changes = _unwrap_changes(whatif)
    if not changes:
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


def role_name_for_guid(guid: str) -> str | None:
    """Return the human-readable name of a built-in role GUID, or None."""
    if not guid:
        return None
    return _BUILTIN_ROLE_GUIDS.get(guid.lower())


def _mask_principal(principal_id: str, *, mask: bool) -> str:
    """Optionally mask all but the last 4 chars of a GUID-shaped principal id."""
    if not mask or not principal_id or len(principal_id) <= 4:
        return principal_id
    tail = principal_id[-4:]
    return f"***-{tail}"


def summarise_change(
    change: dict[str, Any],
    *,
    index: int | None = None,
    total: int | None = None,
    mask_principals: bool = False,
) -> str:
    """Human-readable one-line description of a role-assignment deletion.

    When `index`/`total` are provided the line is prefixed with `[i/N]` so
    the deletions are easy to scan in a long preprovision log.
    """
    rid = str(change.get("resourceId") or "<unknown>")
    before = change.get("before") or {}
    props = (
        before.get("properties") if isinstance(before, dict) else None
    ) or {}
    principal_id_raw = str(props.get("principalId") or "")
    principal_id = (
        _mask_principal(principal_id_raw, mask=mask_principals)
        if principal_id_raw
        else "<unknown-principal>"
    )
    principal_type = props.get("principalType") or "<unknown-type>"
    role_def = str(props.get("roleDefinitionId") or "")
    role_guid = role_def.rsplit("/", 1)[-1] if role_def else ""
    role_name = role_name_for_guid(role_guid)
    if role_name:
        role_label = f"{role_name} ({role_guid})"
    elif role_guid:
        role_label = role_guid
    else:
        role_label = "<unknown-role>"
    scope = props.get("scope") or _scope_from_role_assignment_id(rid)
    prefix = "  - "
    if index is not None and total is not None:
        prefix = f"  [{index}/{total}] "
    return (
        f"{prefix}principal={principal_id} ({principal_type}) "
        f"role={role_label} scope={scope} resourceId={rid}"
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
    return raw in _TRUTHY_VALUES


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
    """Load a what-if document from a file path or '-' for stdin.

    JSON parse errors surface as `SystemExit(EXIT_AZ_FAILED)` so `main()`
    can map them to a consistent exit code without re-raising into the
    caller's traceback.
    """
    try:
        if source == "-":
            raw = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as fh:
                raw = fh.read()
    except FileNotFoundError as exc:
        err(f"--from-json file not found: {source}")
        raise SystemExit(EXIT_BAD_ENV) from exc
    except OSError as exc:
        err(f"failed to read --from-json source {source!r}: {exc}")
        raise SystemExit(EXIT_AZ_FAILED) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # Truncate the snippet so a multi-MB what-if dump does not flood the log.
        snippet = raw[:200].replace("\n", " ")
        err(f"failed to parse what-if JSON from {source}: {exc}; head: {snippet!r}")
        raise SystemExit(EXIT_AZ_FAILED) from exc


def compute_whatif(
    *,
    subscription: str,
    location: str,
    template_file: str,
    parameters: list[str],
    extra_args: Iterable[str] = (),
    runner: Any = None,
) -> dict[str, Any]:
    """Invoke `az deployment sub what-if` and return the parsed JSON.

    `runner` is an optional `subprocess.run`-compatible callable so unit
    tests can stub the az invocation without monkey-patching the module.
    """
    if not os.path.isfile(template_file):
        err(f"--template-file does not exist: {template_file}")
        raise SystemExit(EXIT_BAD_ENV)
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
    runner = runner or subprocess.run
    # `runner` is either `subprocess.run` or a test stub; argv is built from
    # validated CLI flags only — no shell, no interpolation.
    proc = runner(
        cmd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        err(
            "az deployment sub what-if failed "
            f"(exit={proc.returncode}); stderr tail: "
            f"{(proc.stderr or '').strip()[-500:]}"
        )
        raise SystemExit(EXIT_AZ_FAILED)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        err(
            f"failed to parse what-if JSON: {exc}; "
            f"stdout tail: {(proc.stdout or '')[-500:]}"
        )
        raise SystemExit(EXIT_AZ_FAILED) from exc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
_HELP_EPILOG = """\
Exit codes:
  0   no removals, OR warn-only mode, OR removal acknowledged
  2   bad CLI usage (mutually exclusive args, missing flag, missing file)
  3   HALT — STRICT_RBAC_REMOVAL_HALT=true and at least one unaccepted removal
  4   az invocation failed or what-if JSON could not be parsed

Examples:
  # Parse a what-if document on disk and report findings (warn-only):
  python scripts/dev/check_rbac_removal.py --from-json /tmp/whatif.json

  # Pipe what-if from az directly:
  az deployment sub what-if --subscription $SID --location $LOC \\
      --template-file infra/main.bicep --no-pretty-print --output json \\
    | python scripts/dev/check_rbac_removal.py --from-json -

  # Enforce halt mode and acknowledge an intentional phase-2 PR:
  STRICT_RBAC_REMOVAL_HALT=true \\
  ACCEPT_RBAC_REMOVAL='phase-2-of-pr-42' \\
    bash scripts/dev/preflight_rbac_removal.sh
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Halt azd provision when a Bicep change would DELETE a "
            "Microsoft.Authorization/roleAssignments resource. Enabled by "
            "STRICT_RBAC_REMOVAL_HALT=true; acknowledge intended removals "
            "with ACCEPT_RBAC_REMOVAL=phase-2-of-pr-NN."
        ),
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    parser.add_argument(
        "--mask-principals",
        action="store_true",
        help=(
            "Mask all but the last 4 characters of principal ids. Use when "
            "the preflight output is uploaded to a CI artifact store."
        ),
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
        code = exc.code
        if isinstance(code, int):
            return code
        return EXIT_AZ_FAILED

    removals = find_rbac_removals(whatif)
    if not removals:
        info("no roleAssignment deletions detected")
        info("SUMMARY: 0 removals (OK)")
        return EXIT_OK

    total = len(removals)
    info(f"detected {total} roleAssignment deletion(s) in what-if:")
    for idx, change in enumerate(removals, start=1):
        print(
            summarise_change(
                change,
                index=idx,
                total=total,
                mask_principals=args.mask_principals,
            ),
            flush=True,
        )

    strict = is_strict_enabled(env)
    accept = acceptance_token(env)

    if not strict:
        warn(
            "STRICT_RBAC_REMOVAL_HALT is OFF — proceeding without halt. "
            "Charter §12a Rule 4 transition will flip this default once "
            "the soak window completes."
        )
        info(f"SUMMARY: {total} removal(s) detected (WARN-ONLY, allowed)")
        return EXIT_OK

    if accept and is_acceptance_valid(accept):
        # Echo the accepted token so it lands in the preprovision log and
        # is grep-able later for audit ("who acknowledged what, when").
        info(
            "ACCEPT_RBAC_REMOVAL satisfied "
            f"(token={accept!r}); proceeding with deployment."
        )
        info(f"SUMMARY: {total} removal(s) detected (ACCEPTED, allowed)")
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
    info(f"SUMMARY: {total} removal(s) detected (HALT)")
    return EXIT_HALT


if __name__ == "__main__":
    sys.exit(main())
