"""Coverage for `scripts/dev/check_rbac_removal.py` — the charter §12a Rule 7
preflight that halts azd provision on roleAssignment deletions.

Responsibility: Asserts the parser correctly classifies what-if change
    entries, the env-var gate is default-OFF, and the ACCEPT_RBAC_REMOVAL
    token regex matches the documented patterns. Pure in-process tests —
    they never call `az` or touch ARM.
Edit boundaries: Add a case here whenever the parser learns to recognise a
    new change shape, a new override token format, or a new exit code.
    Do NOT add integration tests that shell out to `az` — that surface is
    tested in production by the preprovision hook.
Key entry points: `test_find_rbac_removals_*`, `test_main_*`,
    `test_is_acceptance_valid_*`.
Risky contracts: The script lives outside the `api/` import tree, so we
    import it via `importlib.util.spec_from_file_location` to avoid adding
    `scripts/` to `sys.path` for every test session.
Validation: `uv run pytest -q api/tests/test_check_rbac_removal.py`.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve()
_SCRIPT_PATH = _HERE.parent.parent.parent / "scripts" / "dev" / "check_rbac_removal.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_rbac_removal", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crr = _load_module()


# ---------------------------------------------------------------------------
# find_rbac_removals
# ---------------------------------------------------------------------------
def _change(
    *,
    change_type: str,
    resource_id: str,
    before_props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "changeType": change_type,
        "resourceId": resource_id,
    }
    if before_props is not None:
        out["before"] = {"properties": before_props}
    return out


_RA_RID = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/"
    "rg-elb-dashboard/providers/Microsoft.Authorization/roleAssignments/"
    "11111111-1111-1111-1111-111111111111"
)
_NON_RA_RID = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/"
    "rg-elb-dashboard/providers/Microsoft.Storage/storageAccounts/stelbabc"
)


def test_find_rbac_removals_empty_changes() -> None:
    assert crr.find_rbac_removals({"changes": []}) == []
    assert crr.find_rbac_removals({}) == []
    assert crr.find_rbac_removals({"properties": {"changes": []}}) == []


def test_find_rbac_removals_unwraps_properties_envelope() -> None:
    change = _change(change_type="Delete", resource_id=_RA_RID)
    out = crr.find_rbac_removals({"properties": {"changes": [change]}})
    assert out == [change]


def test_find_rbac_removals_filters_non_role_assignments() -> None:
    change = _change(change_type="Delete", resource_id=_NON_RA_RID)
    assert crr.find_rbac_removals({"changes": [change]}) == []


@pytest.mark.parametrize(
    "change_type", ["Create", "Modify", "NoChange", "Ignore", "Deploy"]
)
def test_find_rbac_removals_filters_non_delete_change_types(change_type: str) -> None:
    change = _change(change_type=change_type, resource_id=_RA_RID)
    assert crr.find_rbac_removals({"changes": [change]}) == []


@pytest.mark.parametrize("change_type", ["Delete", "DEPLOYMENTMODE", "delete"])
def test_find_rbac_removals_accepts_removal_change_types(change_type: str) -> None:
    change = _change(change_type=change_type, resource_id=_RA_RID)
    out = crr.find_rbac_removals({"changes": [change]})
    assert out == [change]


def test_find_rbac_removals_garbage_input_returns_empty() -> None:
    assert crr.find_rbac_removals(None) == []  # type: ignore[arg-type]
    assert crr.find_rbac_removals({"changes": "not-a-list"}) == []
    assert crr.find_rbac_removals({"changes": [None, 42, "x"]}) == []


# ---------------------------------------------------------------------------
# summarise_change
# ---------------------------------------------------------------------------
def test_summarise_change_extracts_principal_and_role() -> None:
    change = _change(
        change_type="Delete",
        resource_id=_RA_RID,
        before_props={
            "principalId": "22222222-2222-2222-2222-222222222222",
            "principalType": "ServicePrincipal",
            "roleDefinitionId": (
                "/subscriptions/00000000-0000-0000-0000-000000000001/providers/"
                "Microsoft.Authorization/roleDefinitions/"
                "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
            ),
            "scope": "/subscriptions/00000000-0000-0000-0000-000000000001",
        },
    )
    summary = crr.summarise_change(change)
    assert "22222222-2222-2222-2222-222222222222" in summary
    assert "ServicePrincipal" in summary
    assert "ba92f5b4-2d11-453d-a403-e96b0029c9fe" in summary
    assert "/subscriptions/00000000-0000-0000-0000-000000000001" in summary


def test_summarise_change_handles_missing_before() -> None:
    change = _change(change_type="Delete", resource_id=_RA_RID)
    summary = crr.summarise_change(change)
    assert "<unknown-principal>" in summary
    assert "<unknown-role>" in summary
    # Scope is derived from the resource id when no `before` is present.
    assert "/resourceGroups/rg-elb-dashboard" in summary


# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("enabled", True),
        ("ENABLED", True),
        ("", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("disabled", False),
        ("anything-else", False),
    ],
)
def test_is_strict_enabled(raw: str, expected: bool) -> None:
    assert crr.is_strict_enabled({"STRICT_RBAC_REMOVAL_HALT": raw}) is expected


@pytest.mark.parametrize(
    "token",
    [
        "phase-2-of-pr-42",
        "phase 2 of pr-42",
        "phase-2 of 2 (see PR-42)",
        "phase-2 of 2 (see #42)",
        "phase-2-of-pr-1234",
        "PHASE-2-OF-PR-42",
    ],
)
def test_is_acceptance_valid_accepts_documented_patterns(token: str) -> None:
    assert crr.is_acceptance_valid(token) is True


@pytest.mark.parametrize(
    "token",
    [
        "",
        "true",
        "yes-please",
        "phase-1-of-pr-42",
        "phase 2",
        "see PR-42",
        "phase-2-of-pr-",
    ],
)
def test_is_acceptance_valid_rejects_other_strings(token: str) -> None:
    assert crr.is_acceptance_valid(token) is False


# ---------------------------------------------------------------------------
# main() — exit-code matrix
# ---------------------------------------------------------------------------
def _write_whatif(tmp_path: Path, payload: dict[str, Any]) -> Path:
    target = tmp_path / "whatif.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_main_returns_ok_when_no_removals(tmp_path: Path) -> None:
    payload = {"changes": [_change(change_type="Create", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    rc = crr.main(["--from-json", str(path)], env={})
    assert rc == crr.EXIT_OK


def test_main_warn_only_when_strict_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    rc = crr.main(["--from-json", str(path)], env={})
    out = capsys.readouterr().out
    assert rc == crr.EXIT_OK
    assert "STRICT_RBAC_REMOVAL_HALT is OFF" in out


def test_main_halts_when_strict_on_and_no_accept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    rc = crr.main(
        ["--from-json", str(path)],
        env={"STRICT_RBAC_REMOVAL_HALT": "true"},
    )
    out = capsys.readouterr().out
    assert rc == crr.EXIT_HALT
    assert "Refusing to deploy" in out
    assert "ACCEPT_RBAC_REMOVAL" in out


def test_main_proceeds_when_accept_token_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    rc = crr.main(
        ["--from-json", str(path)],
        env={
            "STRICT_RBAC_REMOVAL_HALT": "true",
            "ACCEPT_RBAC_REMOVAL": "phase-2-of-pr-42",
        },
    )
    out = capsys.readouterr().out
    assert rc == crr.EXIT_OK
    assert "ACCEPT_RBAC_REMOVAL satisfied" in out


def test_main_halts_when_accept_token_is_garbage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    rc = crr.main(
        ["--from-json", str(path)],
        env={
            "STRICT_RBAC_REMOVAL_HALT": "true",
            "ACCEPT_RBAC_REMOVAL": "yes please",
        },
    )
    out = capsys.readouterr().out
    assert rc == crr.EXIT_HALT
    assert "does not match the required" in out


def test_main_reads_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = crr.main(["--from-json", "-"], env={"STRICT_RBAC_REMOVAL_HALT": "true"})
    assert rc == crr.EXIT_HALT


def test_main_compute_requires_subscription_and_location() -> None:
    rc = crr.main(["--compute"], env={})
    assert rc == crr.EXIT_BAD_ENV


# ---------------------------------------------------------------------------
# Robustness additions (charter §12a Rule 7 hardening wave)
# ---------------------------------------------------------------------------
def test_find_rbac_removals_unwraps_doubly_wrapped_envelope() -> None:
    """Some Azure SDK / REST shapes nest properties twice."""
    change = _change(change_type="Delete", resource_id=_RA_RID)
    payload = {"properties": {"properties": {"changes": [change]}}}
    assert crr.find_rbac_removals(payload) == [change]


def test_summarise_change_includes_builtin_role_name() -> None:
    """Storage Blob Data Contributor GUID should be expanded to a name."""
    change = _change(
        change_type="Delete",
        resource_id=_RA_RID,
        before_props={
            "principalId": "22222222-2222-2222-2222-222222222222",
            "principalType": "ServicePrincipal",
            "roleDefinitionId": (
                "/subscriptions/00000000-0000-0000-0000-000000000001/providers/"
                "Microsoft.Authorization/roleDefinitions/"
                "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
            ),
        },
    )
    summary = crr.summarise_change(change)
    assert "Storage Blob Data Contributor" in summary
    assert "ba92f5b4-2d11-453d-a403-e96b0029c9fe" in summary


def test_role_name_for_guid_known_and_unknown() -> None:
    assert crr.role_name_for_guid("8e3af657-a8ff-443c-a75c-2fe8c4bcb635") == "Owner"
    # Case-insensitive.
    assert (
        crr.role_name_for_guid("B24988AC-6180-42A0-AB88-20F7382DD24C")
        == "Contributor"
    )
    assert crr.role_name_for_guid("00000000-0000-0000-0000-000000000000") is None
    assert crr.role_name_for_guid("") is None


def test_summarise_change_masks_principal_when_flagged() -> None:
    change = _change(
        change_type="Delete",
        resource_id=_RA_RID,
        before_props={
            "principalId": "22222222-2222-2222-2222-222222229999",
            "principalType": "ServicePrincipal",
        },
    )
    plain = crr.summarise_change(change)
    masked = crr.summarise_change(change, mask_principals=True)
    assert "22222222-2222-2222-2222-222222229999" in plain
    assert "22222222-2222-2222-2222-222222229999" not in masked
    assert "***-9999" in masked


def test_summarise_change_index_prefix_renders() -> None:
    change = _change(change_type="Delete", resource_id=_RA_RID)
    summary = crr.summarise_change(change, index=2, total=5)
    assert summary.startswith("  [2/5] ")


def test_main_emits_summary_sentinel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    crr.main(["--from-json", str(path)], env={})
    out = capsys.readouterr().out
    assert "SUMMARY:" in out
    assert "(WARN-ONLY" in out


def test_main_summary_for_zero_removals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Create", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    crr.main(["--from-json", str(path)], env={})
    out = capsys.readouterr().out
    assert "SUMMARY: 0 removals (OK)" in out


def test_main_summary_for_halt_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    crr.main(
        ["--from-json", str(path)],
        env={"STRICT_RBAC_REMOVAL_HALT": "true"},
    )
    out = capsys.readouterr().out
    assert "SUMMARY:" in out
    assert "(HALT)" in out


def test_main_summary_for_accepted_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    crr.main(
        ["--from-json", str(path)],
        env={
            "STRICT_RBAC_REMOVAL_HALT": "true",
            "ACCEPT_RBAC_REMOVAL": "phase-2-of-pr-42",
        },
    )
    out = capsys.readouterr().out
    assert "ACCEPT_RBAC_REMOVAL satisfied" in out
    assert "(ACCEPTED, allowed)" in out


def test_main_missing_from_json_file_returns_bad_env(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = crr.main(["--from-json", "/nonexistent/whatif.json"], env={})
    out = capsys.readouterr().out
    assert rc == crr.EXIT_BAD_ENV
    assert "--from-json file not found" in out


def test_main_malformed_json_returns_az_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json at all", encoding="utf-8")
    rc = crr.main(["--from-json", str(bad)], env={})
    out = capsys.readouterr().out
    assert rc == crr.EXIT_AZ_FAILED
    assert "failed to parse what-if JSON" in out


def test_main_argparse_unknown_flag_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        crr.main(["--no-such-flag"], env={})
    assert excinfo.value.code == 2


def test_main_argparse_missing_source_exits_two() -> None:
    """`--from-json` and `--compute` are mutually exclusive AND required."""
    with pytest.raises(SystemExit) as excinfo:
        crr.main([], env={})
    assert excinfo.value.code == 2


def test_compute_whatif_invokes_az_with_expected_args(
    tmp_path: Path,
) -> None:
    """Mock subprocess so we can validate argv shape without calling az."""
    calls: list[Any] = []

    template = tmp_path / "main.bicep"
    template.write_text("// fake", encoding="utf-8")

    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}

    class _Proc:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _Proc:
        calls.append((cmd, kwargs))
        return _Proc()

    out = crr.compute_whatif(
        subscription="sub-1",
        location="koreacentral",
        template_file=str(template),
        parameters=["envName=dev"],
        runner=fake_run,
    )
    assert out == payload
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[0:4] == ["az", "deployment", "sub", "what-if"]
    assert "--subscription" in cmd and "sub-1" in cmd
    assert "--location" in cmd and "koreacentral" in cmd
    assert "--template-file" in cmd and str(template) in cmd
    # Each --parameters appears as a separate flag pair.
    assert cmd.count("--parameters") == 1
    assert "envName=dev" in cmd
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True


def test_compute_whatif_az_failure_raises_az_failed(tmp_path: Path) -> None:
    template = tmp_path / "main.bicep"
    template.write_text("// fake", encoding="utf-8")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "azure-cli: ResourceGroupNotFound"

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _Proc:
        return _Proc()

    with pytest.raises(SystemExit) as excinfo:
        crr.compute_whatif(
            subscription="sub-1",
            location="koreacentral",
            template_file=str(template),
            parameters=[],
            runner=fake_run,
        )
    assert excinfo.value.code == crr.EXIT_AZ_FAILED


def test_compute_whatif_missing_template_raises_bad_env(tmp_path: Path) -> None:
    missing = tmp_path / "absent.bicep"
    with pytest.raises(SystemExit) as excinfo:
        crr.compute_whatif(
            subscription="sub-1",
            location="koreacentral",
            template_file=str(missing),
            parameters=[],
            runner=lambda *a, **k: None,  # never called
        )
    assert excinfo.value.code == crr.EXIT_BAD_ENV


def test_compute_whatif_malformed_stdout_raises_az_failed(tmp_path: Path) -> None:
    template = tmp_path / "main.bicep"
    template.write_text("// fake", encoding="utf-8")

    class _Proc:
        returncode = 0
        stdout = "not json"
        stderr = ""

    with pytest.raises(SystemExit) as excinfo:
        crr.compute_whatif(
            subscription="sub-1",
            location="koreacentral",
            template_file=str(template),
            parameters=[],
            runner=lambda *a, **k: _Proc(),
        )
    assert excinfo.value.code == crr.EXIT_AZ_FAILED


def test_help_epilog_contains_exit_code_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        crr.main(["--help"], env={})
    out = capsys.readouterr().out
    assert "Exit codes:" in out
    # Spot-check three of the documented exit codes.
    assert "0   no removals" in out
    assert "3   HALT" in out
    assert "4   az invocation failed" in out


def test_main_emits_accept_token_for_audit_grep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The accepted ACCEPT_RBAC_REMOVAL value lands in the log verbatim so
    'git log + grep' can answer 'who/when acknowledged removal X?' later."""
    payload = {"changes": [_change(change_type="Delete", resource_id=_RA_RID)]}
    path = _write_whatif(tmp_path, payload)
    crr.main(
        ["--from-json", str(path)],
        env={
            "STRICT_RBAC_REMOVAL_HALT": "true",
            "ACCEPT_RBAC_REMOVAL": "phase-2-of-pr-123",
        },
    )
    out = capsys.readouterr().out
    assert "phase-2-of-pr-123" in out
    assert "ACCEPT_RBAC_REMOVAL satisfied" in out
