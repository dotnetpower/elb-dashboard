#!/usr/bin/env bash
set -euo pipefail

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out_dir="/workspace/evidence/eq06-web-rid-probe-${stamp}"
mkdir -p "$out_dir"

python3 - <<'PY' "$out_dir"
from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

OUT_DIR = pathlib.Path(sys.argv[1])
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BLAST = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"
USER_AGENT = "elb-dashboard-eq06-probe/1.0"

CASES = [
    {
        "id": "18s_fungal",
        "database": "TL/18S_fungal_sequences",
        "local_database": "18S_fungal_sequences",
        "max_query_bases": 300,
        "entrez_terms": [
            "Saccharomyces cerevisiae[Organism] 18S ribosomal RNA[Title] RefSeq[filter]",
            "Saccharomyces cerevisiae[Organism] 18S ribosomal RNA[Title] NOT pdb[filter]",
            "Fungi[Organism] 18S ribosomal RNA[Title] RefSeq[filter]",
        ],
    },
    {
        "id": "its_refseq_fungi",
        "database": "rRNA_typestrains/ITS_RefSeq_Fungi",
        "local_database": "ITS_RefSeq_Fungi",
        "entrez_terms": [
            "Saccharomyces cerevisiae[Organism] internal transcribed spacer[Title] RefSeq[filter]",
            "Fungi[Organism] internal transcribed spacer[Title] RefSeq[filter]",
            "Saccharomyces cerevisiae[Organism] internal transcribed spacer[Title]",
        ],
    },
]


def http_text(url: str, *, data: dict[str, str] | None = None, timeout: int = 90) -> str:
    encoded = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"User-Agent": USER_AGENT},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed NCBI endpoints.
        return response.read().decode("utf-8", errors="replace")


def esearch(term: str) -> list[str]:
    query = urllib.parse.urlencode(
        {"db": "nuccore", "term": term, "retmode": "json", "retmax": "5", "sort": "relevance"}
    )
    payload = json.loads(http_text(f"{EUTILS}/esearch.fcgi?{query}"))
    return list(payload.get("esearchresult", {}).get("idlist", []))


def efetch_fasta(nuccore_id: str) -> str:
    query = urllib.parse.urlencode(
        {"db": "nuccore", "id": nuccore_id, "rettype": "fasta", "retmode": "text"}
    )
    fasta = http_text(f"{EUTILS}/efetch.fcgi?{query}")
    if not fasta.startswith(">"):
        raise RuntimeError(f"efetch did not return FASTA for {nuccore_id}: {fasta[:200]}")
    return fasta


def trim_fasta(fasta: str, max_query_bases: object) -> str:
    if not isinstance(max_query_bases, int) or max_query_bases <= 0:
        return fasta
    lines = fasta.splitlines()
    header = lines[0] if lines and lines[0].startswith(">") else ">query"
    sequence = "".join(line.strip() for line in lines if not line.startswith(">"))
    trimmed = sequence[:max_query_bases]
    wrapped = "\n".join(trimmed[index : index + 80] for index in range(0, len(trimmed), 80))
    return f"{header} first_{len(trimmed)}bp\n{wrapped}\n"


def choose_query(case: dict[str, object]) -> dict[str, str]:
    attempts: list[dict[str, object]] = []
    for term in case["entrez_terms"]:  # type: ignore[index]
        ids = esearch(str(term))
        attempts.append({"term": term, "ids": ids})
        for nuccore_id in ids:
            try:
                fasta = efetch_fasta(nuccore_id)
            except Exception as exc:  # noqa: BLE001 - evidence probe records failures.
                attempts.append({"id": nuccore_id, "error": str(exc)[:300]})
                continue
            sequence = "".join(line.strip() for line in fasta.splitlines() if not line.startswith(">"))
            if len(sequence) >= 100:
                fasta = trim_fasta(fasta, case.get("max_query_bases"))
                return {"nuccore_id": nuccore_id, "fasta": fasta, "attempts": json.dumps(attempts)}
    raise RuntimeError(f"no usable query found: {attempts}")


def submit_blast(*, database: str, query_fasta: str) -> tuple[str, str]:
    body = {
        "CMD": "Put",
        "PROGRAM": "blastn",
        "DATABASE": database,
        "QUERY": query_fasta,
        "MEGABLAST": "on",
        "EXPECT": "10",
        "WORD_SIZE": "28",
        "HITLIST_SIZE": "500",
        "FILTER": "L",
        "FORMAT_TYPE": "XML",
    }
    text = http_text(BLAST, data=body, timeout=120)
    match = re.search(r"RID\s*=\s*([A-Z0-9]+)", text)
    if not match:
        raise RuntimeError(f"RID not found in BLAST response: {text[:800]}")
    return match.group(1), text


def poll_status(rid: str, *, timeout_seconds: int = 2700) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        query = urllib.parse.urlencode({"CMD": "Get", "RID": rid, "FORMAT_OBJECT": "SearchInfo"})
        text = http_text(f"{BLAST}?{query}", timeout=90)
        status_match = re.search(r"Status=(\w+)", text)
        status = status_match.group(1) if status_match else "UNKNOWN_RESPONSE"
        if status in {"READY", "FAILED", "UNKNOWN"}:
            return status
        time.sleep(20)
    return "TIMEOUT"


def fetch_xml(rid: str) -> str:
    query = urllib.parse.urlencode({"CMD": "Get", "RID": rid, "FORMAT_TYPE": "XML"})
    xml_text = http_text(f"{BLAST}?{query}", timeout=180)
    if "<BlastOutput" not in xml_text:
        raise RuntimeError(f"RID {rid} did not return BLAST XML: {xml_text[:800]}")
    return xml_text


def summarize_xml(xml_text: str) -> dict[str, object]:
    root = ET.fromstring(xml_text)  # noqa: S314 - NCBI XML evidence probe.
    hits = root.findall(".//Hit")
    hsps = root.findall(".//Hsp")
    stats = root.find(".//Iteration_stat/Statistics")
    return {
        "blast_version": root.findtext("BlastOutput_version"),
        "hit_count": len(hits),
        "hsp_count": len(hsps),
        "top_hit_id": hits[0].findtext("Hit_id") if hits else None,
        "top_hit_accession": hits[0].findtext("Hit_accession") if hits else None,
        "statistics_db_len": stats.findtext("Statistics_db-len") if stats is not None else None,
        "statistics_db_num": stats.findtext("Statistics_db-num") if stats is not None else None,
        "statistics_eff_space": stats.findtext("Statistics_eff-space") if stats is not None else None,
    }


def write_text(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


results: list[dict[str, object]] = []
for case in CASES:
    case_dir = OUT_DIR / str(case["id"])
    case_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {
        "id": case["id"],
        "database": case["database"],
        "local_database": case["local_database"],
    }
    try:
        query = choose_query(case)
        write_text(case_dir / "query.fa", query["fasta"])
        result["query_nuccore_id"] = query["nuccore_id"]
        result["query_attempts"] = json.loads(query["attempts"])
        rid, submit_response = submit_blast(database=str(case["database"]), query_fasta=query["fasta"])
        write_text(case_dir / "submit-response.txt", submit_response)
        result["rid"] = rid
        status = poll_status(rid)
        result["status"] = status
        if status == "READY":
            xml_text = fetch_xml(rid)
            write_text(case_dir / "web.xml", xml_text)
            result.update(summarize_xml(xml_text))
            result["positive_hits"] = int(result.get("hit_count") or 0) > 0
        else:
            result["positive_hits"] = False
    except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, ET.ParseError) as exc:
        result["status"] = "ERROR"
        result["error"] = str(exc)[:1000]
    write_text(case_dir / "summary.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
    results.append(result)

overall = {
    "probe": "EQ-06 Web RID capture",
    "case_count": len(results),
    "positive_ready_cases": sum(1 for row in results if row.get("status") == "READY" and row.get("positive_hits")),
    "results": results,
}
write_text(OUT_DIR / "summary.json", json.dumps(overall, indent=2, sort_keys=True) + "\n")
print(json.dumps(overall, indent=2, sort_keys=True))
PY
