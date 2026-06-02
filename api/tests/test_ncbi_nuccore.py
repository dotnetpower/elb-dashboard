"""Tests for the NCBI nuccore service + /api/ncbi/nuccore routes.

Responsibility: Cover the esummary parse, GenBank XML parse (including XXE
rejection + malformed input), FASTA fetch (with and without subrange), the TTL
cache (re-entry must not hit the network), accession normalisation, and the
three FastAPI routes (200 / 422 / 503 / dev-bypass auth).
Edit boundaries: Only services in `api/services/ncbi/`, route in
`api/routes/ncbi.py`. No live network access.
Key entry points: `fetch_nuccore_summary`, `fetch_nuccore_genbank`,
`fetch_nuccore_fasta`, `normalise_accession`, `/api/ncbi/nuccore/{acc}`.
Risky contracts: The cache is process-global; every test must clear it.
Validation: `uv run pytest -q api/tests/test_ncbi_nuccore.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

_GENBANK_XML = b"""<?xml version="1.0" ?>
<GBSet><GBSeq>
  <GBSeq_locus>NM_000546</GBSeq_locus>
  <GBSeq_length>2629</GBSeq_length>
  <GBSeq_moltype>mRNA</GBSeq_moltype>
  <GBSeq_topology>linear</GBSeq_topology>
  <GBSeq_strandedness>single</GBSeq_strandedness>
  <GBSeq_division>PRI</GBSeq_division>
  <GBSeq_update-date>15-OCT-2024</GBSeq_update-date>
  <GBSeq_create-date>27-FEB-1995</GBSeq_create-date>
  <GBSeq_definition>Homo sapiens tumor protein p53 (TP53), mRNA</GBSeq_definition>
  <GBSeq_accession-version>NM_000546.6</GBSeq_accession-version>
  <GBSeq_source>Homo sapiens (human)</GBSeq_source>
  <GBSeq_organism>Homo sapiens</GBSeq_organism>
  <GBSeq_taxonomy>Eukaryota; Metazoa; Chordata; Mammalia; Primates; Hominidae; Homo</GBSeq_taxonomy>
  <GBSeq_comment>REVIEWED REFSEQ: This record has been curated by NCBI staff.</GBSeq_comment>
  <GBSeq_xrefs>
    <GBXref><GBXref_dbname>BioProject</GBXref_dbname><GBXref_id>PRJNA12345</GBXref_id></GBXref>
    <GBXref><GBXref_dbname>BioSample</GBXref_dbname><GBXref_id>SAMN00000001</GBXref_id></GBXref>
  </GBSeq_xrefs>
  <GBSeq_references>
    <GBReference>
      <GBReference_title>p53: At the crossroads of cell-cycle regulation</GBReference_title>
      <GBReference_journal>Nature Reviews 2 (8), 594-604 (2002)</GBReference_journal>
      <GBReference_authors>
        <GBAuthor>Vogelstein,B.</GBAuthor>
        <GBAuthor>Lane,D.</GBAuthor>
        <GBAuthor>Levine,A.J.</GBAuthor>
      </GBReference_authors>
      <GBReference_pubmed>12154352</GBReference_pubmed>
    </GBReference>
  </GBSeq_references>
  <GBSeq_feature-table>
    <GBFeature>
      <GBFeature_key>source</GBFeature_key>
      <GBFeature_location>1..2629</GBFeature_location>
      <GBFeature_intervals>
        <GBInterval>
          <GBInterval_from>1</GBInterval_from>
          <GBInterval_to>2629</GBInterval_to>
        </GBInterval>
      </GBFeature_intervals>
      <GBFeature_quals>
        <GBQualifier><GBQualifier_name>organism</GBQualifier_name>
          <GBQualifier_value>Homo sapiens</GBQualifier_value></GBQualifier>
        <GBQualifier><GBQualifier_name>chromosome</GBQualifier_name>
          <GBQualifier_value>17</GBQualifier_value></GBQualifier>
      </GBFeature_quals>
    </GBFeature>
    <GBFeature>
      <GBFeature_key>CDS</GBFeature_key>
      <GBFeature_location>203..1384</GBFeature_location>
      <GBFeature_intervals>
        <GBInterval>
          <GBInterval_from>203</GBInterval_from>
          <GBInterval_to>1384</GBInterval_to>
        </GBInterval>
      </GBFeature_intervals>
      <GBFeature_quals>
        <GBQualifier><GBQualifier_name>gene</GBQualifier_name>
          <GBQualifier_value>TP53</GBQualifier_value></GBQualifier>
        <GBQualifier><GBQualifier_name>product</GBQualifier_name>
          <GBQualifier_value>cellular tumor antigen p53</GBQualifier_value></GBQualifier>
      </GBFeature_quals>
    </GBFeature>
  </GBSeq_feature-table>
</GBSeq></GBSet>
"""

_ESUMMARY_JSON: dict[str, Any] = {
    "header": {"type": "esummary", "version": "0.3"},
    "result": {
        "uids": ["NM_000546.6"],
        "NM_000546.6": {
            "uid": "568815587",
            "caption": "NM_000546",
            "title": "Homo sapiens tumor protein p53 (TP53), mRNA",
            "accessionversion": "NM_000546.6",
            "slen": 2629,
            "biomol": "mRNA",
            "moltype": "rna",
            "topology": "linear",
            "sourcedb": "refseq",
            "strand": "single",
            "completeness": "complete",
            "createdate": "1995/02/27",
            "updatedate": "2024/10/15",
            "taxid": 9606,
            "organism": "Homo sapiens",
        },
    },
}


def _clear_caches() -> None:
    from api.services.ncbi import clear_nuccore_caches

    clear_nuccore_caches()


# ---------------------------------------------------------------------------
# normalise_accession
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NM_000546", "NM_000546"),
        ("NM_000546.6", "NM_000546.6"),
        ("nm_000546.6", "NM_000546.6"),
        (" NM_000546.6 ", "NM_000546.6"),
        ("gi|123|ref|NM_000546.6|", "NM_000546.6"),
        ("U12345", "U12345"),
        ("AB123456.1", "AB123456.1"),
        ("XR_001234567.2", "XR_001234567.2"),
        ("NC_012920", "NC_012920"),
    ],
)
def test_normalise_accession_accepts_known_shapes(raw: str, expected: str) -> None:
    from api.services.ncbi import normalise_accession

    assert normalise_accession(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "drop-table;",
        "../../etc/passwd",
        "NM_xxx",
        "NM_000546.",
        "A" * 64,
        "NM_000546 OR 1=1",
        "{}",
        123,
        None,
        True,
    ],
)
def test_normalise_accession_rejects_invalid(raw: Any) -> None:
    from api.services.ncbi import normalise_accession

    with pytest.raises(ValueError):
        normalise_accession(raw)


# ---------------------------------------------------------------------------
# esummary
# ---------------------------------------------------------------------------
def test_fetch_nuccore_summary_parses_record(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    from api.services.ncbi import nuccore

    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request_json(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        calls.append((endpoint, params))
        return _ESUMMARY_JSON

    monkeypatch.setattr(nuccore, "request_json", fake_request_json)

    result = nuccore.fetch_nuccore_summary("NM_000546.6")

    assert calls == [
        ("esummary.fcgi", {"db": "nuccore", "id": "NM_000546.6", "retmode": "json"})
    ]
    assert result["accession"] == "NM_000546"
    assert result["accession_version"] == "NM_000546.6"
    assert result["length"] == 2629
    assert result["organism"] == "Homo sapiens"
    assert result["taxid"] == 9606
    assert result["moltype"] == "rna"
    assert result["cached"] is False
    assert result["source"] == "ncbi_eutils"


def test_fetch_nuccore_summary_caches_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    from api.services.ncbi import nuccore

    invocations: list[int] = []

    def fake_request_json(_endpoint: str, _params: dict[str, str]) -> dict[str, Any]:
        invocations.append(1)
        return _ESUMMARY_JSON

    monkeypatch.setattr(nuccore, "request_json", fake_request_json)
    first = nuccore.fetch_nuccore_summary("nm_000546.6")
    second = nuccore.fetch_nuccore_summary("NM_000546.6")
    assert len(invocations) == 1
    assert first["cached"] is False
    assert second["cached"] is True
    # Cached dict must not alias the bucket.
    second["title"] = "tampered"
    third = nuccore.fetch_nuccore_summary("NM_000546.6")
    assert third["title"].startswith("Homo sapiens tumor protein p53")


def test_fetch_nuccore_summary_handles_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    from api.services.ncbi import NcbiServiceUnavailable, nuccore

    monkeypatch.setattr(
        nuccore,
        "request_json",
        lambda *_a, **_kw: {
            "result": {"uids": [], "error": "ID NM_999999 not found"},
        },
    )
    with pytest.raises(NcbiServiceUnavailable):
        nuccore.fetch_nuccore_summary("NM_999999")


# ---------------------------------------------------------------------------
# GenBank XML
# ---------------------------------------------------------------------------
def test_fetch_nuccore_genbank_parses_record(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    from api.services.ncbi import nuccore

    captured: list[tuple[str, dict[str, str], int]] = []

    def fake_request_bytes(
        endpoint: str, params: dict[str, str], *, max_bytes: int, accept: str = ""
    ) -> bytes:
        del accept
        captured.append((endpoint, params, max_bytes))
        return _GENBANK_XML

    monkeypatch.setattr(nuccore, "request_bytes", fake_request_bytes)

    record = nuccore.fetch_nuccore_genbank("NM_000546.6")
    assert captured == [
        (
            "efetch.fcgi",
            {
                "db": "nuccore",
                "id": "NM_000546.6",
                "rettype": "gb",
                "retmode": "xml",
            },
            nuccore.MAX_GENBANK_BYTES,
        )
    ]
    assert record["accession_version"] == "NM_000546.6"
    assert record["length"] == 2629
    assert record["organism"] == "Homo sapiens"
    assert record["division"] == "PRI"
    assert record["definition"].startswith("Homo sapiens tumor protein p53")
    assert record["moltype"] == "mrna"
    assert record["features"][0]["key"] == "source"
    assert record["features"][1]["key"] == "CDS"
    cds = record["features"][1]
    assert cds["from"] == 203
    assert cds["to"] == 1384
    assert cds["strand"] == "plus"
    qual_names = [q["name"] for q in cds["qualifiers"]]
    assert "gene" in qual_names
    assert record["references"][0]["pubmed"] == "12154352"
    assert record["xrefs"] == [
        {"dbname": "BioProject", "id": "PRJNA12345"},
        {"dbname": "BioSample", "id": "SAMN00000001"},
    ]
    assert record["cached"] is False


def test_fetch_nuccore_genbank_rejects_xxe(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    from api.services.ncbi import NcbiServiceUnavailable, nuccore

    xxe = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
        b"<GBSet><GBSeq><GBSeq_locus>&x;</GBSeq_locus></GBSeq></GBSet>"
    )
    monkeypatch.setattr(
        nuccore, "request_bytes", lambda *_a, **_kw: xxe
    )
    with pytest.raises(NcbiServiceUnavailable):
        nuccore.fetch_nuccore_genbank("NM_000546.6")


def test_fetch_nuccore_genbank_handles_malformed_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    from api.services.ncbi import NcbiServiceUnavailable, nuccore

    monkeypatch.setattr(
        nuccore, "request_bytes", lambda *_a, **_kw: b"not xml at all"
    )
    with pytest.raises(NcbiServiceUnavailable):
        nuccore.fetch_nuccore_genbank("NM_000546.6")


def test_fetch_nuccore_genbank_requires_gbseq_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    from api.services.ncbi import NcbiServiceUnavailable, nuccore

    monkeypatch.setattr(
        nuccore,
        "request_bytes",
        lambda *_a, **_kw: b'<?xml version="1.0"?><GBSet></GBSet>',
    )
    with pytest.raises(NcbiServiceUnavailable):
        nuccore.fetch_nuccore_genbank("NM_000546.6")


# ---------------------------------------------------------------------------
# FASTA
# ---------------------------------------------------------------------------
def test_fetch_nuccore_fasta_passes_subrange(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    from api.services.ncbi import nuccore

    seen: list[tuple[str, dict[str, str]]] = []

    def fake_request_bytes(
        endpoint: str, params: dict[str, str], **_kw: Any
    ) -> bytes:
        seen.append((endpoint, params))
        return b">NM_000546.6 fake\nACGT\nACGT\n"

    monkeypatch.setattr(nuccore, "request_bytes", fake_request_bytes)

    text = nuccore.fetch_nuccore_fasta(
        "NM_000546.6", seq_start=100, seq_stop=200
    )
    assert text.startswith(">NM_000546.6")
    assert seen == [
        (
            "efetch.fcgi",
            {
                "db": "nuccore",
                "id": "NM_000546.6",
                "rettype": "fasta",
                "retmode": "text",
                "seq_start": "100",
                "seq_stop": "200",
            },
        )
    ]


def test_fetch_nuccore_fasta_rejects_partial_subrange() -> None:
    from api.services.ncbi import nuccore

    _clear_caches()
    with pytest.raises(ValueError):
        nuccore.fetch_nuccore_fasta("NM_000546.6", seq_start=100, seq_stop=None)
    with pytest.raises(ValueError):
        nuccore.fetch_nuccore_fasta("NM_000546.6", seq_start=None, seq_stop=200)
    with pytest.raises(ValueError):
        nuccore.fetch_nuccore_fasta("NM_000546.6", seq_start=0, seq_stop=10)


def test_fetch_nuccore_fasta_rejects_non_fasta_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    from api.services.ncbi import NcbiServiceUnavailable, nuccore

    monkeypatch.setattr(
        nuccore, "request_bytes", lambda *_a, **_kw: b"ID NM_999 not found"
    )
    with pytest.raises(NcbiServiceUnavailable):
        nuccore.fetch_nuccore_fasta("NM_000546.6")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def test_route_summary_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import ncbi
    from api.services.ncbi import nuccore

    def fake_fetch(accession: str) -> dict[str, Any]:
        assert accession == "NM_000546.6"
        return {
            "accession": "NM_000546",
            "accession_version": "NM_000546.6",
            "length": 2629,
            "cached": False,
        }

    monkeypatch.setattr(nuccore, "fetch_nuccore_summary", fake_fetch)
    monkeypatch.setattr(ncbi, "fetch_nuccore_summary", fake_fetch)

    response = TestClient(app).get("/api/ncbi/nuccore/NM_000546.6")

    assert response.status_code == 200
    assert response.json()["accession_version"] == "NM_000546.6"


def test_route_summary_rejects_bad_accession(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    response = TestClient(app).get("/api/ncbi/nuccore/not-an-accession")
    assert response.status_code == 422
    detail = response.json()
    assert detail["code"] == "ncbi_accession_invalid"


def test_route_summary_maps_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import ncbi
    from api.services.ncbi import nuccore

    def fake_fetch(_accession: str) -> dict[str, Any]:
        raise ncbi.NcbiServiceUnavailable("eutils down")

    monkeypatch.setattr(nuccore, "fetch_nuccore_summary", fake_fetch)
    monkeypatch.setattr(ncbi, "fetch_nuccore_summary", fake_fetch)

    response = TestClient(app).get("/api/ncbi/nuccore/NM_000546.6")
    assert response.status_code == 503
    detail = response.json()
    assert detail["code"] == "ncbi_lookup_unavailable"
    assert detail["retryable"] is True


def test_route_genbank_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import ncbi
    from api.services.ncbi import nuccore

    def fake_fetch(_accession: str) -> dict[str, Any]:
        return {
            "accession_version": "NM_000546.6",
            "features": [{"key": "CDS"}],
            "cached": False,
        }

    monkeypatch.setattr(nuccore, "fetch_nuccore_genbank", fake_fetch)
    monkeypatch.setattr(ncbi, "fetch_nuccore_genbank", fake_fetch)

    response = TestClient(app).get("/api/ncbi/nuccore/NM_000546.6/genbank")
    assert response.status_code == 200
    assert response.json()["features"][0]["key"] == "CDS"


def test_route_fasta_returns_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import ncbi
    from api.services.ncbi import nuccore

    def fake_fetch(
        accession: str, *, seq_start: int | None, seq_stop: int | None
    ) -> str:
        assert accession == "NM_000546.6"
        assert seq_start == 100
        assert seq_stop == 200
        return ">NM_000546.6:100-200\nACGT\n"

    monkeypatch.setattr(nuccore, "fetch_nuccore_fasta", fake_fetch)
    monkeypatch.setattr(ncbi, "fetch_nuccore_fasta", fake_fetch)

    response = TestClient(app).get(
        "/api/ncbi/nuccore/NM_000546.6/fasta?seq_start=100&seq_stop=200"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-fasta")
    assert response.text.startswith(">NM_000546.6:100-200")


def test_route_fasta_rejects_invalid_subrange(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    # seq_start=0 violates ge=1 — FastAPI returns 422 before touching the service.
    response = TestClient(app).get(
        "/api/ncbi/nuccore/NM_000546.6/fasta?seq_start=0&seq_stop=10"
    )
    assert response.status_code == 422


def test_route_summary_requires_auth_when_not_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "false")
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    from api.main import app

    response = TestClient(app).get("/api/ncbi/nuccore/NM_000546.6")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# LRU cache + NcbiResponseTooLarge mapping
# ---------------------------------------------------------------------------
def test_summary_cache_evicts_in_lru_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filling the cache past `_MAX_CACHE_ENTRIES` must drop the *oldest*
    accession, and a `_cache_get` on an older entry must move it back to the
    fresh end so it survives the next eviction."""
    _clear_caches()
    from api.services.ncbi import nuccore

    # Shrink the cap so we don't have to materialise 512 dummy records.
    monkeypatch.setattr(nuccore, "_MAX_CACHE_ENTRIES", 3)

    def make(accession: str) -> dict[str, Any]:
        payload = dict(_ESUMMARY_JSON)
        payload = {
            "result": {
                "uids": [accession],
                accession: {
                    "uid": "1",
                    "caption": accession.split(".")[0],
                    "title": f"title for {accession}",
                    "accessionversion": accession,
                    "slen": 100,
                    "biomol": "mRNA",
                    "moltype": "rna",
                    "topology": "linear",
                    "sourcedb": "refseq",
                    "strand": "single",
                    "completeness": "complete",
                    "createdate": "2024/01/01",
                    "updatedate": "2024/01/01",
                    "taxid": 9606,
                    "organism": "Homo sapiens",
                },
            },
        }
        return payload

    monkeypatch.setattr(
        nuccore,
        "request_json",
        lambda _ep, params: make(params["id"]),
    )

    nuccore.fetch_nuccore_summary("NM_000001.1")  # cache: [001]
    nuccore.fetch_nuccore_summary("NM_000002.1")  # cache: [001, 002]
    nuccore.fetch_nuccore_summary("NM_000003.1")  # cache: [001, 002, 003]

    # Touch 001 — it should move to the fresh end.
    cached_first = nuccore.fetch_nuccore_summary("NM_000001.1")
    assert cached_first["cached"] is True

    # Insert a 4th: cap is 3, so the LRU entry (now 002, since 001 was bumped)
    # must be evicted.
    nuccore.fetch_nuccore_summary("NM_000004.1")  # cache: [003, 001, 004]

    # 002 was evicted -> hitting it again triggers a fresh fetch.
    refetched_002 = nuccore.fetch_nuccore_summary("NM_000002.1")
    assert refetched_002["cached"] is False

    # 001 was protected by the LRU touch -> still cached.
    still_cached_001 = nuccore.fetch_nuccore_summary("NM_000001.1")
    assert still_cached_001["cached"] is True


def test_summary_cache_isolation_after_deepcopy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutation on a returned dict (or its nested list) must not poison the
    next cache hit. Guards against regressions that drop the read-side
    `deepcopy`."""
    _clear_caches()
    from api.services.ncbi import nuccore

    monkeypatch.setattr(
        nuccore, "request_json", lambda *_a, **_kw: _ESUMMARY_JSON
    )

    first = nuccore.fetch_nuccore_summary("NM_000546.6")
    assert first["cached"] is False
    # Mutate top-level + nested list reference returned by the cache.
    first["title"] = "tampered"
    if isinstance(first.get("taxonomy_lineage"), list):
        first["taxonomy_lineage"].append("INJECTED")

    second = nuccore.fetch_nuccore_summary("NM_000546.6")
    assert second["title"].startswith("Homo sapiens tumor protein p53")
    if isinstance(second.get("taxonomy_lineage"), list):
        assert "INJECTED" not in second["taxonomy_lineage"]


def test_fasta_overflow_raises_response_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When NCBI returns a FASTA body that exceeds `MAX_FASTA_BYTES`,
    `fetch_nuccore_fasta` must surface `NcbiResponseTooLarge` (a
    `NcbiServiceUnavailable` subclass) so the BLAST submit bridge can map it
    to 422 `ncbi_query_too_large` rather than the retryable 503."""
    _clear_caches()
    from api.services.ncbi import NcbiResponseTooLarge, nuccore

    def fake_request_bytes(*_a: Any, **_kw: Any) -> bytes:
        raise NcbiResponseTooLarge("FASTA exceeds cap")

    monkeypatch.setattr(nuccore, "request_bytes", fake_request_bytes)

    with pytest.raises(NcbiResponseTooLarge):
        nuccore.fetch_nuccore_fasta("NM_000546.6")


# ---------------------------------------------------------------------------
# Cross-process Redis token bucket (#1)
# ---------------------------------------------------------------------------


def test_redis_token_bucket_acquires_when_redis_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The api + worker sidecars share a Redis-backed bucket so the
    aggregate rate cannot exceed the NCBI policy. When Redis responds
    `acquired=1` the consumer must not touch the in-process bucket at
    all."""
    from api.services.ncbi import _eutils

    class _FakeRedis:
        def __init__(self) -> None:
            self.eval_calls = 0
            self.evalsha_calls = 0

        def evalsha(self, *_a: object, **_kw: object) -> list[int]:
            self.evalsha_calls += 1
            return [1, 0]

        def eval(self, *_a: object, **_kw: object) -> list[int]:  # pragma: no cover
            self.eval_calls += 1
            return [1, 0]

    fake = _FakeRedis()
    monkeypatch.setattr(_eutils, "_redis_bucket_client", lambda: fake)
    _eutils.reset_rate_limiter()
    _eutils._consume_token(timeout_seconds=0.5)
    assert fake.evalsha_calls == 1


def test_redis_token_bucket_rate_limited_signals_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Redis says the bucket is empty and the deadline passes, the
    consumer must raise `NcbiRateLimited` so the route maps it to 429
    rather than waiting indefinitely."""
    from api.services.ncbi import _eutils
    from api.services.ncbi._eutils import NcbiRateLimited

    class _BusyRedis:
        def evalsha(self, *_a: object, **_kw: object) -> list[int]:
            return [0, 50]

    monkeypatch.setattr(_eutils, "_redis_bucket_client", lambda: _BusyRedis())
    _eutils.reset_rate_limiter()
    with pytest.raises(NcbiRateLimited):
        _eutils._consume_token(timeout_seconds=0.1)


def test_redis_token_bucket_falls_back_on_redis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis EVAL exception must trip the breaker and fall back to
    the in-process bucket so a Redis outage does not block fetches."""
    from api.services.ncbi import _eutils

    class _ExplodingRedis:
        def evalsha(self, *_a: object, **_kw: object) -> list[int]:
            raise RuntimeError("redis is down")

        def eval(self, *_a: object, **_kw: object) -> list[int]:
            raise RuntimeError("redis is down")

    monkeypatch.setattr(_eutils, "_redis_bucket_client", lambda: _ExplodingRedis())
    _eutils.reset_rate_limiter()
    # No exception — in-process bucket has tokens left so this returns
    # quickly. The test would hang or raise if the fallback did not
    # engage.
    _eutils._consume_token(timeout_seconds=0.5)


# ---------------------------------------------------------------------------
# Per-caller quota (critique #10, #11, #13, #19)
# ---------------------------------------------------------------------------
def test_caller_bucket_key_namespaces_dev_bypass_by_upn() -> None:
    """Critique #10: two dev-bypass identities with distinct upns must not
    collide in the per-caller bucket so concurrent local dashboards do not
    starve each other."""
    from api.auth import DEV_BYPASS_OID, CallerIdentity
    from api.routes.ncbi import _caller_bucket_key

    a = CallerIdentity(
        object_id=DEV_BYPASS_OID,
        tenant_id="t",
        upn="alice@local",
        raw_token="",
        claims={"dev_bypass": True},
    )
    b = CallerIdentity(
        object_id=DEV_BYPASS_OID,
        tenant_id="t",
        upn="bob@local",
        raw_token="",
        claims={"dev_bypass": True},
    )
    assert _caller_bucket_key(a) != _caller_bucket_key(b)
    assert _caller_bucket_key(a).startswith("dev-bypass:")


def test_caller_bucket_key_real_caller_uses_oid() -> None:
    from api.auth import CallerIdentity
    from api.routes.ncbi import _caller_bucket_key

    caller = CallerIdentity(
        object_id="11111111-2222-3333-4444-555555555555",
        tenant_id="t",
        upn="real@user",
        raw_token="tok",
        claims={},
    )
    assert _caller_bucket_key(caller) == "11111111-2222-3333-4444-555555555555"


def test_caller_quota_rejects_empty_oid_with_401() -> None:
    """Critique #10: real caller with empty oid must NOT silently share an
    'anonymous' bucket with every other empty-oid caller — 401 instead."""
    from api.auth import CallerIdentity
    from api.routes.ncbi import _check_caller_quota, _reset_caller_quota_for_tests
    from fastapi import HTTPException

    _reset_caller_quota_for_tests()
    caller = CallerIdentity(
        object_id="",
        tenant_id="t",
        upn="",
        raw_token="tok",
        claims={},
    )
    with pytest.raises(HTTPException) as exc_info:
        _check_caller_quota(caller)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "missing_caller_identity"


def test_caller_bucket_lru_evicts_when_over_soft_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critique #11: the per-caller dict must not grow unboundedly.

    Forces the cap to a low value so the test runs in <50ms.
    """
    from api.auth import CallerIdentity
    from api.routes import ncbi as ncbi_routes

    monkeypatch.setattr(ncbi_routes, "_CALLER_BUCKETS_MAX_KEYS", 4)
    ncbi_routes._reset_caller_quota_for_tests()
    for i in range(10):
        ncbi_routes._check_caller_quota(
            CallerIdentity(
                object_id=f"00000000-0000-0000-0000-00000000000{i}",
                tenant_id="t",
                upn="",
                raw_token="tok",
                claims={},
            )
        )
    assert len(ncbi_routes._CALLER_BUCKETS) <= 4


def test_caller_quota_refund_pops_most_recent_timestamp() -> None:
    """Critique #13: refund must drop the slot just charged so a tight
    client retrying on shared-bucket 429 does not pay double quota."""
    from api.auth import CallerIdentity
    from api.routes.ncbi import (
        _CALLER_BUCKETS,
        _check_caller_quota,
        _refund_caller_quota,
        _reset_caller_quota_for_tests,
    )

    _reset_caller_quota_for_tests()
    caller = CallerIdentity(
        object_id="aaaaaaaa-1111-2222-3333-444444444444",
        tenant_id="t",
        upn="real@user",
        raw_token="tok",
        claims={},
    )
    key = _check_caller_quota(caller)
    assert len(_CALLER_BUCKETS[key]) == 1
    _refund_caller_quota(key)
    # Bucket entirely empty AND key garbage-collected from dict.
    assert key not in _CALLER_BUCKETS


def test_caller_quota_refund_handles_unknown_key() -> None:
    """Refund must be a no-op when the key is empty or already absent."""
    from api.routes.ncbi import _refund_caller_quota, _reset_caller_quota_for_tests

    _reset_caller_quota_for_tests()
    _refund_caller_quota("")  # empty key
    _refund_caller_quota("nonexistent")  # unknown key — must not raise


def test_caller_buckets_guard_is_initialised_at_module_load() -> None:
    """Critique #19: the lock must be initialised at import time, not lazily.

    Lazy init has a microsecond-scale race where two threads see ``None``
    simultaneously and construct independent locks; the loser's
    ``acquire`` then does not serialise with the winner's. Module-level
    init costs one ``Lock()`` constructor call at import and is the
    safer pattern.
    """
    from api.routes import ncbi as ncbi_routes

    # Lock object — not None — even before any quota call.
    assert ncbi_routes._CALLER_BUCKETS_GUARD is not None
    # Acquiring it must not raise.
    with ncbi_routes._CALLER_BUCKETS_GUARD:
        pass


def test_route_summary_refunds_quota_on_shared_bucket_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end refund: an upstream 429 must not consume the caller's slot."""
    _clear_caches()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import ncbi as ncbi_routes
    from api.services import ncbi as ncbi_service
    from api.services.ncbi import NcbiRateLimited, nuccore

    ncbi_routes._reset_caller_quota_for_tests()

    def fake_fetch(_accession: str) -> dict[str, Any]:
        raise NcbiRateLimited("shared bucket exhausted")

    monkeypatch.setattr(nuccore, "fetch_nuccore_summary", fake_fetch)
    monkeypatch.setattr(ncbi_service, "fetch_nuccore_summary", fake_fetch)

    response = TestClient(app).get("/api/ncbi/nuccore/NM_000546.6")
    assert response.status_code == 429
    assert response.json()["code"] == "ncbi_rate_limited"
    # Bucket must be empty — the refund cleared the slot we briefly
    # reserved before calling NCBI.
    assert all(len(b) == 0 for b in ncbi_routes._CALLER_BUCKETS.values())
