"""AST regression guard: HTTPException(detail=str(exc)) must be sanitised.

Responsibility: Block raw `str(exc)` from reaching `HTTPException(detail=…)`
without going through `api.services.sanitise.sanitise(…)[:limit]`. Exception
text leaking SAS URLs, bearer tokens, GUIDs, or connection strings into HTTP
responses is the audit P1 #7 / #8 finding.
Edit boundaries: Scan-only. Do not import FastAPI or any route module — the
test must run in isolation in any environment that can parse Python.
Key entry points: `_HTTP_EXC_ALLOWLIST` (escape hatch for legitimate raw text
such as static error messages), `test_routes_sanitise_httpexception_detail`,
`test_routes_sanitise_exc_does_not_leak_secrets`.
Risky contracts: The whitelist must stay tiny — every entry needs a Justification
comment so future hardening PRs can audit it. New raw `str(exc)` sites added
to a route file under `api/routes/` will be rejected with a clear message
pointing at the line.
Validation: `uv run pytest -q api/tests/test_httpexception_sanitise.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from api.services.sanitise import sanitise

ROUTES_DIR = Path(__file__).resolve().parent.parent / "routes"


# Function names that produce sanitised output. A call to any of these wrapping
# `str(exc)` (or just `exc`) is treated as compliant.
_SANITISING_FUNCTIONS = frozenset({"sanitise"})


# Allowlist: relative path under `api/routes/` → justification string. Each
# entry MUST come with a Justification comment explaining why raw `str(exc)`
# is intentional. Empty by default — every audit PR that touches HTTP error
# surfaces should keep this set small.
_HTTP_EXC_ALLOWLIST: dict[str, str] = {}


def _scan_targets() -> list[Path]:
    """Return all `*.py` files under `api/routes/` (recursive)."""
    return sorted(ROUTES_DIR.rglob("*.py"))


def _is_str_of_exc(node: ast.AST) -> bool:
    """Match `str(exc)` and `str(exc)[:N]` patterns."""
    if isinstance(node, ast.Subscript):
        return _is_str_of_exc(node.value)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "str" and len(node.args) == 1:
            # Match str(<anything>) — the typical pattern is str(exc) but we
            # also catch str(some_attr) which is equally unsafe.
            return True
    return False


def _is_sanitised_call(node: ast.AST) -> bool:
    """Match `sanitise(...)[:N]` or `sanitise(...)`."""
    if isinstance(node, ast.Subscript):
        return _is_sanitised_call(node.value)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _SANITISING_FUNCTIONS:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _SANITISING_FUNCTIONS:
            return True
    return False


def _detail_arg_leaks_raw_exc(node: ast.AST) -> bool:
    """Recursively classify a `detail=` value as raw-exc or sanitised.

    Returns True when the AST node embeds a raw `str(exc)` / `str(exc)[:N]`
    that is NOT already wrapped in a sanitising call.
    """
    if _is_sanitised_call(node):
        return False
    if _is_str_of_exc(node):
        return True
    # f-string interpolation — recurse into formatted values.
    if isinstance(node, ast.JoinedStr):
        return any(
            _detail_arg_leaks_raw_exc(v.value)
            for v in node.values
            if isinstance(v, ast.FormattedValue)
        )
    # Conditional expression — both branches must be safe.
    if isinstance(node, ast.IfExp):
        return _detail_arg_leaks_raw_exc(node.body) or _detail_arg_leaks_raw_exc(node.orelse)
    # Subscript slicing of any other node — recurse into value.
    if isinstance(node, ast.Subscript):
        return _detail_arg_leaks_raw_exc(node.value)
    return False


def _httpexception_violations(text: str) -> list[str]:
    """Return human-readable descriptions of every offending HTTPException.

    A site is offending when an HTTPException call has a `detail` kwarg or
    second positional arg that contains `str(exc)` without a sanitising wrap.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match HTTPException(...) by name. We deliberately accept both the
        # bare `HTTPException` and any `xxx.HTTPException` form so a future
        # alias does not silently bypass the guard.
        if not (
            (isinstance(func, ast.Name) and func.id == "HTTPException")
            or (isinstance(func, ast.Attribute) and func.attr == "HTTPException")
        ):
            continue
        # detail kwarg
        detail_node: ast.AST | None = None
        for kw in node.keywords:
            if kw.arg == "detail":
                detail_node = kw.value
                break
        # Second positional arg (legacy HTTPException(status_code, detail))
        if detail_node is None and len(node.args) >= 2:
            detail_node = node.args[1]
        if detail_node is None:
            continue
        if _detail_arg_leaks_raw_exc(detail_node):
            violations.append(f"line {node.lineno}: {ast.unparse(detail_node)}")
    return violations


_SCAN_TARGETS = _scan_targets()


@pytest.mark.parametrize("path", _SCAN_TARGETS, ids=lambda p: str(p.relative_to(ROUTES_DIR)))
def test_routes_sanitise_httpexception_detail(path: Path) -> None:
    """No route module may pass raw `str(exc)` to an HTTPException `detail`."""
    rel = path.relative_to(ROUTES_DIR).as_posix()
    text = path.read_text(encoding="utf-8")
    violations = _httpexception_violations(text)
    if rel in _HTTP_EXC_ALLOWLIST:
        assert violations == [], (
            f"{rel} is in _HTTP_EXC_ALLOWLIST but still leaks raw str(exc). "
            f"Justification: '{_HTTP_EXC_ALLOWLIST[rel]}'. Sites: {violations}"
        )
        return
    assert violations == [], (
        f"{rel} returns raw `str(exc)` in HTTPException detail. Wrap each "
        "occurrence with `sanitise(str(exc))[:200]` from "
        "`api.services.sanitise` so SAS URLs, bearer tokens, connection "
        f"strings, and GUIDs do not leak to clients. Sites: {violations}"
    )


def test_sanitise_masks_common_secret_payloads_used_in_exc_detail() -> None:
    """`sanitise` removes the SAS / Bearer / connection-string shapes that
    Azure SDK ValueErrors typically carry — proving the wrap chosen by every
    audit-P1 #7/#8 site actually defangs the payload before it reaches the
    HTTP response."""
    sas = (
        "blob fetch failed: "
        "https://acc.blob.core.windows.net/c/k?sv=2024-01-01&sr=b&sig=ABCDEFGHIJKLM12345"
    )
    bearer = "auth failed: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
    conn = (
        "init failed: "
        "DefaultEndpointsProtocol=https;AccountName=acc;AccountKey=AAAAAAAAAAAAAAAAAAAA=="
    )
    guid = "missing principal 11111111-2222-3333-4444-555555555555"
    for raw in (sas, bearer, conn, guid):
        out = sanitise(raw)
        assert "<redacted>" in out or "sig=<redacted>" in out or "…" in out or "redacted" in out, (
            f"sanitise did not mask payload: in={raw!r} out={out!r}"
        )
        # Specific anti-leak assertions:
        assert "ABCDEFGHIJKLM12345" not in out, "SAS sig leaked through sanitise"
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig" not in out, (
            "Bearer token leaked through sanitise"
        )
        assert "AAAAAAAAAAAAAAAAAAAA" not in out, "AccountKey leaked through sanitise"
        guid_leak = "11111111-2222-3333-4444-555555555555" not in out
        assert guid_leak, "Full GUID leaked through sanitise"
