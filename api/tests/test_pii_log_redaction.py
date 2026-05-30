"""PII redaction guard for caller GUID logging.

Responsibility: Lock down the 30-item audit P0 #1 finding — `caller.object_id`
    must never appear as a raw value (no wrapping `redact_oid` /
    `_log_identity_hash` call, no `[-8:]` slice) in any positional argument
    to `LOGGER.*`. The check is AST-based so it understands nested calls and
    multi-line invocations the way a human reader would.
Edit boundaries: Pure regression test. Do not extend by importing the routes
    or running them — the guard is a static AST scan so it catches drift
    without booting FastAPI / Celery / Azure mocks.
Key entry points: `test_routes_do_not_log_raw_caller_object_id`,
    `test_redact_oid_is_stable_and_non_reversible`,
    `test_redact_oid_output_is_not_a_guid_substring`.
Risky contracts: If a new route legitimately needs the raw GUID (e.g. an
    audit row partition key persisted to Azure Table, never a log line),
    add the file to `_RAW_OID_ALLOWLIST` with a one-line justification.
    The default stance is "always redact in logs".
Validation: `uv run pytest -q api/tests/test_pii_log_redaction.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTES_DIR = REPO_ROOT / "api" / "routes"

_SCAN_TARGETS = sorted(ROUTES_DIR.rglob("*.py"))

# Files where raw `caller.object_id` legitimately appears in a non-logging
# position (e.g. ticket payload field, audit row PartitionKey). Logging in
# these files is still checked — this allowlist only documents the file was
# reviewed for non-log raw usage.
_RAW_OID_ALLOWLIST: dict[str, str] = {}

# Function names whose output is considered already-redacted. Adding to this
# set is a deliberate widening — review carefully.
_REDACTING_FUNCTIONS = frozenset({"redact_oid", "_log_identity_hash"})


def _is_raw_caller_object_id(node: ast.AST) -> bool:
    """True iff `node` is literally `caller.object_id` (raw attribute load)."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "object_id"
        and isinstance(node.value, ast.Name)
        and node.value.id == "caller"
    )


def _is_redacted_call(node: ast.AST) -> bool:
    """True iff `node` is a Call to a known redaction function."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _REDACTING_FUNCTIONS
    if isinstance(func, ast.Attribute):
        return func.attr in _REDACTING_FUNCTIONS
    return False


def _logger_calls(tree: ast.AST) -> list[ast.Call]:
    """Yield every `LOGGER.<level>(...)` call in `tree`."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not isinstance(func.value, ast.Name):
            continue
        if func.value.id != "LOGGER":
            continue
        out.append(node)
    return out


def _arg_leaks_raw_oid(arg: ast.AST) -> bool:
    """Recursively True iff `arg` evaluates `caller.object_id` without
    passing it through a recognised redactor first."""
    if _is_raw_caller_object_id(arg):
        return True

    # A redacting call neutralises everything inside it.
    if _is_redacted_call(arg):
        return False

    # `caller.object_id[-8:]` or `caller.object_id[:8]` — slicing is a
    # partial redaction the audit explicitly accepted.
    if isinstance(arg, ast.Subscript) and _is_raw_caller_object_id(arg.value):
        return False

    # f-string parts.
    if isinstance(arg, ast.JoinedStr):
        return any(_arg_leaks_raw_oid(part) for part in arg.values)
    if isinstance(arg, ast.FormattedValue):
        return _arg_leaks_raw_oid(arg.value)

    for child in ast.iter_child_nodes(arg):
        if _arg_leaks_raw_oid(child):
            return True
    return False


def _raw_oid_logger_violations(text: str) -> list[str]:
    """Return human-readable descriptions of LOGGER.* calls that pass a
    raw `caller.object_id` as any of their args."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"<unparseable: {exc}>"]

    violations: list[str] = []
    for call in _logger_calls(tree):
        suspect_args: list[str] = []
        for arg in (*call.args, *(kw.value for kw in call.keywords)):
            if _arg_leaks_raw_oid(arg):
                suspect_args.append(ast.unparse(arg))
        if suspect_args:
            violations.append(f"line {call.lineno}: {suspect_args}")
    return violations


@pytest.mark.parametrize("path", _SCAN_TARGETS, ids=lambda p: str(p.relative_to(ROUTES_DIR)))
def test_routes_do_not_log_raw_caller_object_id(path: Path) -> None:
    """No route module may pass raw `caller.object_id` to a LOGGER.* call."""
    rel = path.relative_to(ROUTES_DIR).as_posix()
    text = path.read_text(encoding="utf-8")
    violations = _raw_oid_logger_violations(text)
    if rel in _RAW_OID_ALLOWLIST:
        assert violations == [], (
            f"{rel} is in _RAW_OID_ALLOWLIST but still passes raw "
            f"caller.object_id to LOGGER.*. Justification: "
            f"'{_RAW_OID_ALLOWLIST[rel]}'. Offending sites: {violations}"
        )
        return
    assert violations == [], (
        f"{rel} logs raw caller.object_id (PII GUID). Wrap each occurrence "
        "with `redact_oid(caller.object_id)` from `api.services.sanitise`. "
        f"Offending sites: {violations}"
    )


def test_redact_oid_is_stable_and_non_reversible() -> None:
    """`redact_oid` must be deterministic, short, and never echo the input."""
    from api.services.sanitise import redact_oid

    a = "00000000-0000-0000-0000-000000000000"
    b = "11111111-1111-1111-1111-111111111111"

    out_a = redact_oid(a)
    out_b = redact_oid(b)

    assert out_a is not None
    assert out_b is not None
    assert out_a != a
    assert out_b != b
    assert out_a != out_b
    assert len(out_a) == 12
    assert len(out_b) == 12
    assert redact_oid(a) == out_a
    assert redact_oid(None) is None
    assert redact_oid("") is None


def test_redact_oid_output_is_not_a_guid_substring() -> None:
    """The redacted value must not contain any hex block of the original GUID."""
    from api.services.sanitise import redact_oid

    oid = "abcdef12-3456-7890-abcd-ef1234567890"
    out = redact_oid(oid)
    assert out is not None
    for chunk in oid.split("-"):
        assert chunk not in out, (
            f"redact_oid({oid!r}) returned {out!r} which contains "
            f"original GUID chunk {chunk!r} — reduce input leakage."
        )
