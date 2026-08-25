"""Guard rolling-upgrade network and caller-permission preflights.

Responsibility: Verify the shell preflights accept current Azure response shapes
    and least-privilege platform resource-group roles.
Edit boundaries: CLI upgrade and caller-precheck shell contracts only; no live
    Azure access or deployment mutations.
Key entry points: ``test_*``.
Risky contracts: Approved Storage private endpoints must not be reported as
    absent, and RG-scoped upgrade roles must not weaken subscription-scoped
    deploy, doctor, or RBAC auto-fix requirements.
Validation: ``uv run pytest -q api/tests/test_cli_upgrade_preflight.py``.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CALLER_PRECHECK = _REPO_ROOT / "scripts" / "dev" / "_caller-precheck.sh"
_CLI_UPGRADE = _REPO_ROOT / "scripts" / "dev" / "cli-upgrade.sh"


def _run_precheck(
    mode: str,
    *,
    subscription_role: str = "",
    rg_role: str = "",
) -> subprocess.CompletedProcess[str]:
    command = f"""
source {shlex.quote(str(_CALLER_PRECHECK))}
ELB_CALLER_OID=caller-oid
ELB_CALLER_UPN=caller@example.test
ELB_CALLER_SUB=test-sub
AZURE_RESOURCE_GROUP=platform-rg
az() {{
  local scope="" previous=""
  for argument in "$@"; do
    if [[ "$previous" == "--scope" ]]; then
      scope="$argument"
      break
    fi
    previous="$argument"
  done
  case "$scope" in
    /subscriptions/test-sub) printf '%s\\n' {shlex.quote(subscription_role)} ;;
    /subscriptions/test-sub/resourceGroups/platform-rg) printf '%s\\n' {shlex.quote(rg_role)} ;;
  esac
}}
elb_precheck_caller_for {shlex.quote(mode)}
"""
    return subprocess.run(  # noqa: S603 -- executes a repository-controlled shell helper.
        ["/bin/bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )


def _private_endpoint_count(payload: dict[str, object]) -> str:
    script = _CLI_UPGRADE.read_text(encoding="utf-8")
    match = re.search(
        r"read -r public pe_count.*?\| jq -r '([^']+)'\)",
        script,
        re.DOTALL,
    )
    assert match is not None
    result = subprocess.run(  # noqa: S603 -- executes jq with a repository-controlled expression.
        ["/usr/bin/jq", "-r", match.group(1)],
        check=True,
        capture_output=True,
        input=json.dumps(payload),
        text=True,
    )
    return result.stdout.strip()


def test_storage_parity_accepts_flattened_private_endpoint_shape() -> None:
    assert _private_endpoint_count(
        {
            "public": "Disabled",
            "pecs": [
                {"privateLinkServiceConnectionState": {"status": "Approved"}},
                {"privateLinkServiceConnectionState": {"status": "Rejected"}},
            ],
        }
    ) == "Disabled 1"


def test_storage_parity_accepts_legacy_private_endpoint_shape() -> None:
    assert _private_endpoint_count(
        {
            "public": "Disabled",
            "pecs": [
                {
                    "properties": {
                        "privateLinkServiceConnectionState": {"status": "Approved"}
                    }
                }
            ],
        }
    ) == "Disabled 1"


def test_storage_parity_queries_connections_before_shape_normalisation() -> None:
    script = _CLI_UPGRADE.read_text(encoding="utf-8")
    assert "--query '{public:publicNetworkAccess, pecs:privateEndpointConnections}'" in script


def test_upgrade_read_accepts_reader_at_platform_rg() -> None:
    assert _run_precheck("upgrade-read", rg_role="Reader").returncode == 0


def test_upgrade_write_accepts_contributor_at_platform_rg() -> None:
    assert _run_precheck("upgrade-write", rg_role="Contributor").returncode == 0


def test_upgrade_write_rejects_reader_and_recommends_platform_rg_scope() -> None:
    result = _run_precheck("upgrade-write", rg_role="Reader")
    assert result.returncode == 4
    assert "--scope /subscriptions/test-sub/resourceGroups/platform-rg" in result.stderr


def test_subscription_scoped_modes_do_not_accept_rg_only_roles() -> None:
    assert _run_precheck("deploy", rg_role="Contributor").returncode == 4
    assert _run_precheck("doctor-read", rg_role="Reader").returncode == 4


def test_subscription_role_still_authorizes_upgrade() -> None:
    assert _run_precheck("upgrade-write", subscription_role="Contributor").returncode == 0
