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

from typing import Any, ClassVar

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
  <GBSeq_primary-accession>NM_000546</GBSeq_primary-accession>
  <GBSeq_accession-version>NM_000546.6</GBSeq_accession-version>
  <GBSeq_other-seqids>
    <GBSeqid>ref|NM_000546.6|</GBSeqid>
    <GBSeqid>gi|568815587</GBSeqid>
  </GBSeq_other-seqids>
  <GBSeq_secondary-accessions>
    <GBSecondary-accn>X02469</GBSecondary-accn>
  </GBSeq_secondary-accessions>
  <GBSeq_source>Homo sapiens (human)</GBSeq_source>
  <GBSeq_organism>Homo sapiens</GBSeq_organism>
  <GBSeq_taxonomy>Eukaryota; Metazoa; Chordata; Mammalia; Primates; Hominidae; Homo</GBSeq_taxonomy>
  <GBSeq_keywords>
    <GBKeyword>RefSeq</GBKeyword>
    <GBKeyword>MANE Select</GBKeyword>
  </GBSeq_keywords>
  <GBSeq_comment>REVIEWED REFSEQ: This record has been curated by NCBI staff.</GBSeq_comment>
  <GBSeq_xrefs>
    <GBXref><GBXref_dbname>BioProject</GBXref_dbname><GBXref_id>PRJNA12345</GBXref_id></GBXref>
    <GBXref><GBXref_dbname>BioSample</GBXref_dbname><GBXref_id>SAMN00000001</GBXref_id></GBXref>
  </GBSeq_xrefs>
  <GBSeq_references>
    <GBReference>
      <GBReference_reference>1</GBReference_reference>
      <GBReference_title>p53: At the crossroads of cell-cycle regulation</GBReference_title>
      <GBReference_journal>Nature Reviews 2 (8), 594-604 (2002)</GBReference_journal>
      <GBReference_authors>
        <GBAuthor>Vogelstein,B.</GBAuthor>
        <GBAuthor>Lane,D.</GBAuthor>
        <GBAuthor>Levine,A.J.</GBAuthor>
      </GBReference_authors>
      <GBReference_consortium>TP53 Consortium</GBReference_consortium>
      <GBReference_xref>
        <GBXref><GBXref_dbname>doi</GBXref_dbname><GBXref_id>10.1038/nrc864</GBXref_id></GBXref>
      </GBReference_xref>
      <GBReference_pubmed>12154352</GBReference_pubmed>
      <GBReference_remark>Review article</GBReference_remark>
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
            "status": "live",
            "replacedby": "",
        },
    },
}


def _clear_caches() -> None:
    from api.services.ncbi import clear_nuccore_caches

    clear_nuccore_caches()


@pytest.fixture(autouse=True)
def _disable_durable_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the durable ops-Redis cache for every test by default.

    The durable cache is best-effort and environment-dependent (it talks to
    ``OPS_REDIS_URL``, which defaults to a local Redis that may or may not be
    running). Disabling it keeps the in-process ``cached`` assertions below
    deterministic regardless of whether a local Redis is reachable. The
    dedicated durable-cache tests re-enable it with a fake client.
    """
    monkeypatch.setenv("NCBI_DURABLE_CACHE_DISABLED", "true")
    _clear_caches()


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
        # PDB structure-chain accessions (digit-led 4-char PDB ID + chain).
        # The db=nuccore search returns these for short ssDNA/ssRNA chains, so
        # the fetch path must accept them (regression: live 8WGZ_T rejection).
        ("8WGZ_T", "8WGZ_T"),
        ("8wgz_t", "8WGZ_T"),
        (" 8WGZ_T ", "8WGZ_T"),
        ("1ABC_AB", "1ABC_AB"),
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
    assert result["status"] == "live"
    assert result["replaced_by"] is None
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


class _FakeRedis:
    """Minimal dict-backed Redis stand-in for the durable cache tests."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.get_calls = 0
        self.setex_calls = 0

    def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.setex_calls += 1
        self.store[key] = value


def test_durable_cache_survives_in_process_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable ops-Redis cache must serve a second viewer even after the
    in-process cache is cleared (cold replica / api restart) without a second
    NCBI fetch — this is the core of issue #27."""
    _clear_caches()
    monkeypatch.setenv("NCBI_DURABLE_CACHE_DISABLED", "false")
    from api.services import redis_clients
    from api.services.ncbi import nuccore

    fake = _FakeRedis()
    monkeypatch.setattr(redis_clients, "get_ops_redis_client", lambda **_kw: fake)

    invocations: list[int] = []

    def fake_request_json(_endpoint: str, _params: dict[str, str]) -> dict[str, Any]:
        invocations.append(1)
        return _ESUMMARY_JSON

    monkeypatch.setattr(nuccore, "request_json", fake_request_json)

    first = nuccore.fetch_nuccore_summary("NM_000546.6")
    assert first["cached"] is False
    assert len(invocations) == 1
    assert fake.setex_calls == 1

    # Simulate a cold replica: wipe the in-process cache. The durable cache
    # must now satisfy the read with no extra NCBI call.
    _clear_caches()
    second = nuccore.fetch_nuccore_summary("NM_000546.6")
    assert second["cached"] is True
    assert len(invocations) == 1, "durable cache must avoid a second NCBI fetch"
    assert second["accession_version"] == "NM_000546.6"


def test_durable_cache_degrades_on_redis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any Redis failure must degrade silently to a live NCBI fetch — the
    durable layer can never change correctness, only latency."""
    _clear_caches()
    monkeypatch.setenv("NCBI_DURABLE_CACHE_DISABLED", "false")
    from api.services import redis_clients
    from api.services.ncbi import nuccore

    class _ExplodingRedis:
        def get(self, _key: str) -> str | None:
            raise RuntimeError("redis is down")

        def setex(self, _key: str, _ttl: int, _value: str) -> None:
            raise RuntimeError("redis is down")

    monkeypatch.setattr(
        redis_clients, "get_ops_redis_client", lambda **_kw: _ExplodingRedis()
    )

    invocations: list[int] = []

    def fake_request_json(_endpoint: str, _params: dict[str, str]) -> dict[str, Any]:
        invocations.append(1)
        return _ESUMMARY_JSON

    monkeypatch.setattr(nuccore, "request_json", fake_request_json)

    result = nuccore.fetch_nuccore_summary("NM_000546.6")
    assert result["cached"] is False
    assert result["accession_version"] == "NM_000546.6"
    assert len(invocations) == 1


def test_durable_cache_disabled_env_skips_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the kill-switch set, the durable cache must never touch Redis."""
    _clear_caches()
    monkeypatch.setenv("NCBI_DURABLE_CACHE_DISABLED", "true")
    from api.services import redis_clients
    from api.services.ncbi import nuccore

    fake = _FakeRedis()
    monkeypatch.setattr(redis_clients, "get_ops_redis_client", lambda **_kw: fake)
    monkeypatch.setattr(nuccore, "request_json", lambda *_a, **_kw: _ESUMMARY_JSON)

    nuccore.fetch_nuccore_summary("NM_000546.6")
    assert fake.get_calls == 0
    assert fake.setex_calls == 0


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
    ref0 = record["references"][0]
    assert ref0["reference"] == "1"
    assert ref0["consortium"] == "TP53 Consortium"
    assert ref0["doi"] == "10.1038/nrc864"
    assert ref0["remark"] == "Review article"
    assert record["xrefs"] == [
        {"dbname": "BioProject", "id": "PRJNA12345"},
        {"dbname": "BioSample", "id": "SAMN00000001"},
    ]
    assert record["keywords"] == ["RefSeq", "MANE Select"]
    assert record["gi"] == "568815587"
    assert record["primary_accession"] == "NM_000546"
    assert record["secondary_accessions"] == ["X02469"]
    assert "ref|NM_000546.6|" in record["other_seqids"]
    assert record["cached"] is False
    # Untruncated fixture: no field is clipped and every qualifier reports it.
    assert record["truncated_fields"] == []
    assert all(
        q.get("truncated") is False
        for feature in record["features"]
        for q in feature["qualifiers"]
    )


def test_fetch_nuccore_genbank_returns_full_untruncated_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    from api.services.ncbi import nuccore

    long_comment = "X" * 5000
    long_translation = "M" * 600
    xml = (
        b'<?xml version="1.0" ?>\n<GBSet><GBSeq>'
        b"<GBSeq_locus>NM_000546</GBSeq_locus>"
        b"<GBSeq_accession-version>NM_000546.6</GBSeq_accession-version>"
        b"<GBSeq_definition>Short definition</GBSeq_definition>"
        b"<GBSeq_comment>" + long_comment.encode() + b"</GBSeq_comment>"
        b"<GBSeq_feature-table><GBFeature>"
        b"<GBFeature_key>CDS</GBFeature_key>"
        b"<GBFeature_location>1..1800</GBFeature_location>"
        b"<GBFeature_quals><GBQualifier>"
        b"<GBQualifier_name>translation</GBQualifier_name>"
        b"<GBQualifier_value>" + long_translation.encode() + b"</GBQualifier_value>"
        b"</GBQualifier></GBFeature_quals>"
        b"</GBFeature></GBSeq_feature-table>"
        b"</GBSeq></GBSet>"
    )
    monkeypatch.setattr(nuccore, "request_bytes", lambda *_a, **_kw: xml)

    record = nuccore.fetch_nuccore_genbank("NM_000546.6")
    # Truncation was removed: long fields are returned in full and never flagged.
    assert record["truncated_fields"] == []
    assert record["comment"] == long_comment
    translation_qual = record["features"][0]["qualifiers"][0]
    assert translation_qual["name"] == "translation"
    assert translation_qual["truncated"] is False
    assert translation_qual["value"] == long_translation
    assert not translation_qual["value"].endswith("\u2026")


def test_fetch_nuccore_summary_flags_replaced_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_caches()
    from api.services.ncbi import nuccore

    payload = {
        "result": {
            "uids": ["NM_000546.5"],
            "NM_000546.5": {
                "accessionversion": "NM_000546.5",
                "title": "Homo sapiens tumor protein p53 (TP53), mRNA",
                "status": "replaced",
                "replacedby": "NM_000546.6",
                "taxid": 9606,
                "organism": "Homo sapiens",
            },
        },
    }
    monkeypatch.setattr(nuccore, "request_json", lambda *_a, **_kw: payload)

    result = nuccore.fetch_nuccore_summary("NM_000546.5")
    assert result["status"] == "replaced"
    assert result["replaced_by"] == "NM_000546.6"


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
# efetch (byte-streaming) timeout — large genome records load past 8 s
# ---------------------------------------------------------------------------


def test_efetch_timeout_default_exceeds_summary_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The byte-streaming efetch path must use a budget larger than the
    8 s esummary timeout so a slow-but-healthy genome record (10-20 s
    server-side generation) loads on the first attempt instead of timing
    out and failing with a misleading 503."""
    from api.services.ncbi import _eutils

    monkeypatch.delenv("NCBI_EFETCH_HTTP_TIMEOUT", raising=False)
    timeout = _eutils._efetch_timeout_seconds()
    assert timeout > _eutils.DEFAULT_TIMEOUT_SECONDS
    assert timeout == 30.0


def test_efetch_timeout_env_override_and_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NCBI_EFETCH_HTTP_TIMEOUT` overrides the default, but a value below
    the esummary timeout (or a non-numeric value) is ignored — the efetch
    path must never be made faster-failing than the cheap header call."""
    from api.services.ncbi import _eutils

    monkeypatch.setenv("NCBI_EFETCH_HTTP_TIMEOUT", "45")
    assert _eutils._efetch_timeout_seconds() == 45.0

    monkeypatch.setenv("NCBI_EFETCH_HTTP_TIMEOUT", "2")
    assert _eutils._efetch_timeout_seconds() == _eutils._DEFAULT_EFETCH_TIMEOUT_SECONDS

    monkeypatch.setenv("NCBI_EFETCH_HTTP_TIMEOUT", "not-a-number")
    assert _eutils._efetch_timeout_seconds() == _eutils._DEFAULT_EFETCH_TIMEOUT_SECONDS


def test_request_bytes_passes_efetch_timeout_to_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`request_bytes` must hand the longer efetch timeout to
    `client.stream(...)` per request — not silently inherit the pooled
    client's default 8 s timeout."""
    from api.services.ncbi import _eutils

    captured: dict[str, Any] = {}

    class _FakeResponse:
        headers: ClassVar[dict[str, str]] = {}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [b">seq\nACGT\n"]

        def close(self) -> None:
            return None

    class _FakeStreamCtx:
        def __enter__(self) -> _FakeResponse:
            return _FakeResponse()

        def __exit__(self, *_a: object) -> None:
            return None

    class _FakeClient:
        def stream(self, _method: str, _endpoint: str, **kwargs: Any) -> _FakeStreamCtx:
            captured["timeout"] = kwargs.get("timeout")
            return _FakeStreamCtx()

    monkeypatch.setattr(_eutils, "_consume_token", lambda *a, **k: None)
    monkeypatch.setattr(_eutils, "_pooled_client", lambda _slot: _FakeClient())
    monkeypatch.delenv("NCBI_EFETCH_HTTP_TIMEOUT", raising=False)

    body = _eutils.request_bytes(
        "efetch.fcgi", {"db": "nuccore"}, max_bytes=1024, accept="application/xml"
    )
    assert body == b">seq\nACGT\n"
    assert captured["timeout"] == _eutils._efetch_timeout_seconds()


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


def test_redis_token_bucket_recovers_from_noscript_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Lua script is evicted (e.g. Redis restart), EVALSHA raises
    ``redis.exceptions.NoScriptError`` whose ``str()`` has the "NOSCRIPT"
    prefix stripped by redis-py. The bucket must still fall back to EVAL
    (which reloads + runs the script) instead of degrading to the
    in-process bucket on every call."""
    from api.services.ncbi import _eutils
    from redis.exceptions import NoScriptError

    class _EvictedRedis:
        def __init__(self) -> None:
            self.evalsha_calls = 0
            self.eval_calls = 0

        def evalsha(self, *_a: object, **_kw: object) -> list[int]:
            self.evalsha_calls += 1
            # Match what redis-py 5.x produces — the "NOSCRIPT" prefix
            # is stripped during parsing, leaving only this message.
            raise NoScriptError("No matching script. Please use [E]VAL.")

        def eval(self, *_a: object, **_kw: object) -> list[int]:
            self.eval_calls += 1
            return [1, 0]

    fake = _EvictedRedis()
    monkeypatch.setattr(_eutils, "_redis_bucket_client", lambda: fake)
    _eutils.reset_rate_limiter()
    _eutils._consume_token(timeout_seconds=0.5)
    assert fake.evalsha_calls == 1
    assert fake.eval_calls == 1


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
