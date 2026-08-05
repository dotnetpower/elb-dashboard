"""Tests for the inline-FASTA query-label bridge used by Recent searches.

Responsibility: Lock the behaviour of
    ``api.services.blast.external_query_labels`` — defline derivation, the
    best-effort OPS-Redis remember/recall round trip, and the additive
    enrichment of external job rows (a real ``query_file`` always wins).
Edit boundaries: Test-only. Patches ``get_ops_redis_client`` with an in-memory
    fake; never touches a live Redis.
Key entry points: ``test_*`` functions below.
Risky contracts: ``apply_remembered_query_label`` must be a no-op when the row
    already has a query identity and when nothing is remembered.
Validation: ``uv run pytest -q api/tests/test_external_query_labels.py``.
"""

from __future__ import annotations

import pytest
from api.services.blast import external_query_labels as eql


class _FakeRedis:
    """Minimal in-memory Redis stub implementing only ``set(ex=...)`` / ``get``."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    def get(self, key: str):
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda **_kw: fake
    )
    return fake


def test_derive_single_record_uses_first_defline_id() -> None:
    assert eql.derive_inline_query_label(">NC_003310.1 cowpox\nATGGAGAAG") == "NC_003310.1"


def test_derive_multi_record_appends_count() -> None:
    fasta = ">q1\nATGC\n>q2\nGGGG\n>q3\nCCCC"
    assert eql.derive_inline_query_label(fasta) == "q1 (+2)"


def test_derive_no_header_returns_empty() -> None:
    assert eql.derive_inline_query_label("ATGCATGC") == ""
    assert eql.derive_inline_query_label("") == ""


def test_derive_caps_long_header() -> None:
    long_id = "x" * 500
    label = eql.derive_inline_query_label(f">{long_id}\nATGC")
    assert len(label) == 120
    assert label == "x" * 120


def test_remember_and_recall_round_trip(fake_redis: _FakeRedis) -> None:
    eql.remember_query_label("abc123", "NC_003310.1")
    assert eql.recall_query_label("abc123") == "NC_003310.1"


def test_recall_missing_returns_empty(fake_redis: _FakeRedis) -> None:
    assert eql.recall_query_label("nope") == ""


def test_remember_skips_blank_inputs(fake_redis: _FakeRedis) -> None:
    eql.remember_query_label("", "label")
    eql.remember_query_label("job", "")
    assert fake_redis.store == {}


def test_remember_and_recall_are_best_effort_on_redis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr("api.services.redis_clients.get_ops_redis_client", boom)
    # Neither call raises; recall degrades to empty.
    eql.remember_query_label("abc", "label")
    assert eql.recall_query_label("abc") == ""


def test_remember_inline_round_trip(fake_redis: _FakeRedis) -> None:
    eql.remember_inline_query_label("job-x", ">acc.1 desc\nATGC")
    assert eql.recall_query_label("job-x") == "acc.1"


def test_remember_inline_is_noop_for_non_fasta(fake_redis: _FakeRedis) -> None:
    eql.remember_inline_query_label("job-y", "ATGC-no-header")
    assert fake_redis.store == {}


def test_remember_inline_never_raises_on_derive_failure(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    # A theoretical derive failure must not propagate (would 5xx an accepted
    # submit). Force derive to raise and assert the wrapper swallows it.
    def boom(_fasta: str) -> str:
        raise ValueError("boom")

    monkeypatch.setattr(eql, "derive_inline_query_label", boom)
    eql.remember_inline_query_label("job-z", ">acc.1\nATGC")
    assert fake_redis.store == {}


def test_apply_injects_remembered_label_when_row_has_none(fake_redis: _FakeRedis) -> None:
    eql.remember_query_label("job-1", "NC_003310.1")
    row = {"job_id": "job-1", "program": "blastn"}
    out = eql.apply_remembered_query_label(row)
    assert out["query_file"] == "NC_003310.1"
    # Original row is not mutated.
    assert "query_file" not in row


def test_apply_is_noop_when_row_already_has_query_identity(fake_redis: _FakeRedis) -> None:
    eql.remember_query_label("job-2", "remembered")
    row = {"job_id": "job-2", "query_file": "queries/uploads/real/query.fa"}
    out = eql.apply_remembered_query_label(row)
    assert out is row
    assert out["query_file"] == "queries/uploads/real/query.fa"


def test_apply_is_noop_when_nothing_remembered(fake_redis: _FakeRedis) -> None:
    row = {"job_id": "job-3"}
    out = eql.apply_remembered_query_label(row)
    assert out is row
    assert "query_file" not in out


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "query.fa", "input.fa", "Query.FA", " query.fa "],
)
def test_is_generic_query_label_true_for_placeholders(value) -> None:
    assert eql.is_generic_query_label(value) is True


@pytest.mark.parametrize("value", ["NC_003310.1", "warmup", "q1 (+2)", "my.query.fa"])
def test_is_generic_query_label_false_for_real_deflines(value: str) -> None:
    assert eql.is_generic_query_label(value) is False


def test_derive_masks_a_sas_url_in_the_defline() -> None:
    """The defline is attacker-controlled and the derived label is persisted on
    the job row and rendered in the SPA, so a SAS must never survive it."""
    fasta = (
        ">https://acct.blob.core.windows.net/c/b"
        "?sv=2021-08-06&ss=b&srt=sco&sp=rwdlac&sig=SECRETSIGNATUREVALUE123\n"
        "ACGT\n"
    )
    label = eql.derive_inline_query_label(fasta)
    assert "sig=" not in label
    assert "SECRETSIGNATUREVALUE123" not in label


def test_derive_strips_control_characters() -> None:
    """A NUL cannot be written to an Azure Table property, and ANSI escapes are
    invisible noise in the header."""
    label = eql.derive_inline_query_label(">ab\x00cd\x1b[31mef\nACGT\n")
    assert "\x00" not in label
    assert "\x1b" not in label


def test_derive_returns_empty_when_only_control_chars() -> None:
    assert eql.derive_inline_query_label(">\x00\x01\x02\nACGT\n") == ""


def test_derive_still_caps_after_masking() -> None:
    label = eql.derive_inline_query_label(f">{'x' * 4000}\nACGT")
    assert len(label) <= 120


def test_query_label_miss_marker_round_trip(fake_redis: _FakeRedis) -> None:
    assert eql.query_label_miss_recorded("job-m") is False
    eql.remember_query_label_miss("job-m")
    assert eql.query_label_miss_recorded("job-m") is True
    # The miss marker lives in its own namespace, so the LIST-path recall still
    # reports "nothing remembered" instead of leaking a sentinel into the UI.
    assert eql.recall_query_label("job-m") == ""


def test_query_label_miss_marker_is_best_effort_on_redis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr("api.services.redis_clients.get_ops_redis_client", boom)
    eql.remember_query_label_miss("job-m")  # must not raise
    # Degrades to "not recorded" so the recovery simply re-runs.
    assert eql.query_label_miss_recorded("job-m") is False


def test_query_label_miss_marker_ignores_blank_job_id(fake_redis: _FakeRedis) -> None:
    eql.remember_query_label_miss("")
    assert fake_redis.store == {}
    assert eql.query_label_miss_recorded("") is False
