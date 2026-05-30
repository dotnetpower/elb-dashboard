"""STRICT_AUDIT_HASH redaction tests (audit P2 #13 #14).

Module summary: When `STRICT_AUDIT_HASH=true`, `append_history` walks
the payload dict and replaces values under PII-bearing keys
(caller_oid, owner_oid, upn, …) with `redact_oid()` BEFORE the row is
persisted to the `jobhistory` table. When the flag is unset, payloads
are stored verbatim.

Responsibility: Cover both the ON and OFF paths per charter §12a Rule 4.
Edit boundaries: Exercise the boundary in `repository._redact_audit_payload`
and the gate in `append_history`. JobStateRepository unit tests cover
the rest of the writer behaviour.
Key entry points: per-test functions.
Risky contracts: Default OFF must preserve the legacy payload bytes
exactly. The hash function must be deterministic so historical rows
remain joinable across events without recovering the original GUID.
Validation: `uv run pytest -q api/tests/test_strict_audit_hash.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


def _payload() -> dict[str, Any]:
    return {
        "caller_oid": "alice-oid-1234567890abcdef",
        "owner_oid": "bob-oid-9876543210fedcba",
        "upn": "alice@example.com",
        "subscription_id": "11111111-2222-3333-4444-555555555555",
        "nested": {
            "actor_oid": "carol-oid-deadbeefcafebabe",
            "tags": [
                {"email": "dave@example.com"},
                {"safe_field": "kept-as-is"},
            ],
        },
        "void": "should-not-be-touched",
        "paranoid": "also-not-touched",
        "principal_id": "eve-pid-aaaaaaaaaaaaaaaa",
    }


# ---------------------------------------------------------------------------
# Helper-level tests — _redact_audit_payload is the unit of logic.
# ---------------------------------------------------------------------------


def test_helper_redacts_known_pii_keys() -> None:
    from api.services.state.repository import _redact_audit_payload

    out = _redact_audit_payload(_payload())
    # PII-bearing keys are hashed to sha256[:12] (matches redact_oid).
    assert out["caller_oid"] != "alice-oid-1234567890abcdef"
    assert len(out["caller_oid"]) == 12
    assert out["owner_oid"] != "bob-oid-9876543210fedcba"
    assert out["upn"] != "alice@example.com"
    # Nested dicts walked recursively.
    assert out["nested"]["actor_oid"] != "carol-oid-deadbeefcafebabe"
    assert out["nested"]["tags"][0]["email"] != "dave@example.com"
    # Bare GUIDs under non-PII keys are untouched (subscription_id is
    # operational metadata, not PII; sanitise() handles SAS scrubbing
    # at read time if a path ever ships one).
    assert out["subscription_id"] == "11111111-2222-3333-4444-555555555555"
    # Lookalike key names (void, paranoid) are not flagged.
    assert out["void"] == "should-not-be-touched"
    assert out["paranoid"] == "also-not-touched"
    # principal_id is in the exact-match list.
    assert out["principal_id"] != "eve-pid-aaaaaaaaaaaaaaaa"


def test_helper_is_deterministic() -> None:
    from api.services.state.repository import _redact_audit_payload

    out1 = _redact_audit_payload({"caller_oid": "alice-oid-1234567890abcdef"})
    out2 = _redact_audit_payload({"caller_oid": "alice-oid-1234567890abcdef"})
    assert out1 == out2


def test_helper_preserves_safe_fields() -> None:
    from api.services.state.repository import _redact_audit_payload

    out = _redact_audit_payload(
        {
            "safe_field": "kept",
            "nested": {"another": [1, 2, 3]},
            "tags": [{"a": "b"}],
        }
    )
    assert out["safe_field"] == "kept"
    assert out["nested"]["another"] == [1, 2, 3]
    assert out["tags"][0]["a"] == "b"


def test_helper_handles_empty_and_none_pii_values() -> None:
    from api.services.state.repository import _redact_audit_payload

    out = _redact_audit_payload({"caller_oid": "", "owner_oid": None})
    # Empty / None PII values are not hashed (hashing "" is meaningless
    # and would add noise to historical exports).
    assert out["caller_oid"] == ""
    assert out["owner_oid"] is None


def test_helper_passthrough_for_non_dict_input() -> None:
    from api.services.state.repository import _redact_audit_payload

    assert _redact_audit_payload("string") == "string"
    assert _redact_audit_payload(42) == 42
    assert _redact_audit_payload(None) is None
    assert _redact_audit_payload([1, "two", {"caller_oid": "x"}]) == [
        1,
        "two",
        {"caller_oid": _redact_audit_payload({"caller_oid": "x"})["caller_oid"]},
    ]


# ---------------------------------------------------------------------------
# Integration: append_history honours the gate.
# ---------------------------------------------------------------------------


class _CapturingTableClient:
    def __init__(self) -> None:
        self.entities: list[dict[str, Any]] = []

    def create_entity(self, entity: dict[str, Any]) -> None:
        self.entities.append(entity)

    def __enter__(self) -> _CapturingTableClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


@pytest.fixture()
def _repo(monkeypatch: pytest.MonkeyPatch):
    from api.services.state import repository as repo_mod

    captured = _CapturingTableClient()

    class _Repo(repo_mod.JobStateRepository):
        def __init__(self) -> None:  # bypass __init__ side-effects
            pass

        def _history_client(self) -> _CapturingTableClient:  # type: ignore[override]
            return captured

        def _ensure_table(self, _name: str) -> None:  # type: ignore[override]
            return None

    monkeypatch.delenv("STRICT_AUDIT_HASH", raising=False)
    yield _Repo(), captured


def test_append_history_off_path_writes_verbatim_payload(
    _repo: tuple[Any, _CapturingTableClient],
) -> None:
    repo, captured = _repo
    repo.append_history("job-123", "started", _payload())
    assert len(captured.entities) == 1
    stored = json.loads(captured.entities[0]["payload_json"])
    assert stored["caller_oid"] == "alice-oid-1234567890abcdef"
    assert stored["upn"] == "alice@example.com"


def test_append_history_on_path_hashes_pii(
    _repo: tuple[Any, _CapturingTableClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRICT_AUDIT_HASH", "true")
    repo, captured = _repo
    repo.append_history("job-456", "started", _payload())
    stored = json.loads(captured.entities[0]["payload_json"])
    # PII-bearing keys must be hashed to 12-char fingerprints.
    assert stored["caller_oid"] != "alice-oid-1234567890abcdef"
    assert len(stored["caller_oid"]) == 12
    assert stored["upn"] != "alice@example.com"
    assert stored["nested"]["actor_oid"] != "carol-oid-deadbeefcafebabe"
    assert stored["nested"]["tags"][0]["email"] != "dave@example.com"
    # Non-PII fields preserved.
    assert stored["subscription_id"] == "11111111-2222-3333-4444-555555555555"
    assert stored["void"] == "should-not-be-touched"


def test_append_history_on_path_handles_missing_payload(
    _repo: tuple[Any, _CapturingTableClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Payload=None must not crash (the row is still written, just w/o payload_json)."""
    monkeypatch.setenv("STRICT_AUDIT_HASH", "true")
    repo, captured = _repo
    repo.append_history("job-789", "started", None)
    entity = captured.entities[0]
    assert "payload_json" not in entity
    assert entity["event"] == "started"
