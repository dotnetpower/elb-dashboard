"""Development script for ncbi-searchsp-discovery.

Responsibility: Development script for ncbi-searchsp-discovery
Edit boundaries: Keep this as an operator/dev utility; do not make production code depend on it.
Key entry points: `parse_rows`, `parse_stats`, `submit_to_blastalign`, `fetch_xml`, `run_local`,
`infer_searchsp`
Risky contracts: Assume local developer context only; avoid broad production-side effects.
Validation: `uv run python scripts/dev/ncbi-searchsp-discovery.py --help`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

BASE_OPTIONS = (
    "-task blastn -dust no -evalue 1000 -reward 2 -penalty -3 -gapopen 5 -gapextend 2 -outfmt 5"
)

CASES = [
    {
        "name": "baseline_32nt_4_subjects",
        "query": ">query1\nACGTACGTACGTACGTACGTACGTACGTACGT\n",
        "subjects": """>subject_best
ACGTACGTACGTACGTACGTACGTACGTACGT
>subject_slow
ACGTACGTACGTACGTACGTACGTACGTTCGA
>subject_bit
ACGTACGTACGTACGTACGTACGTACGTACGA
>subject_far
TTTTACGTACGTACGTACGTACGTACGTTTTT
""",
    },
    {
        "name": "longer_64nt_4_subjects",
        "query": ">query1\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n",
        "subjects": """>subject_best
ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT
>subject_one_tail_mismatch
ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGA
>subject_two_block_mismatch
ACGTACGTACGTACGTACGTACGTACGTTCGAACGTACGTACGTACGTACGTACGTACGTACGA
>subject_terminal_noise
TTTTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTTTTT
""",
    },
    {
        "name": "wider_32nt_8_subjects",
        "query": ">query1\nACGTACGTACGTACGTACGTACGTACGTACGT\n",
        "subjects": """>subject_best
ACGTACGTACGTACGTACGTACGTACGTACGT
>subject_bit
ACGTACGTACGTACGTACGTACGTACGTACGA
>subject_slow
ACGTACGTACGTACGTACGTACGTACGTTCGA
>subject_far
TTTTACGTACGTACGTACGTACGTACGTTTTT
>subject_extra_1
GGGGACGTACGTACGTACGTACGTACGTGGGG
>subject_extra_2
CCCCACGTACGTACGTACGTACGTACGTCCCC
>subject_extra_3
AAAACGTACGTACGTACGTACGTACGTAAAA
>subject_extra_4
ACGTACGTACGTACGTAAAACGTACGTACGT
""",
    },
]


def parse_rows(xml_bytes_or_path: bytes | Path) -> list[dict[str, object]]:
    if isinstance(xml_bytes_or_path, bytes):
        root = ET.fromstring(xml_bytes_or_path)  # noqa: S314 - NCBI XML dev probe.
    else:
        root = ET.parse(xml_bytes_or_path).getroot()  # noqa: S314 - Local BLAST XML.
    rows: list[dict[str, object]] = []
    for hit in root.findall(".//Hit"):
        hit_id = hit.findtext("Hit_id") or ""
        hit_def = hit.findtext("Hit_def") or ""
        name = hit_id if hit_id.startswith("subject_") else hit_def.split()[0]
        rows.append(
            {
                "name": name,
                "evalue": float(hit.findtext("./Hit_hsps/Hsp/Hsp_evalue") or "nan"),
                "bitscore": float(hit.findtext("./Hit_hsps/Hsp/Hsp_bit-score") or "nan"),
            }
        )
    return rows


def parse_stats(xml_bytes: bytes) -> dict[str, str | None]:
    root = ET.fromstring(xml_bytes)  # noqa: S314 - NCBI XML dev probe.
    stats = root.find(".//Iteration_stat/Statistics")
    params = root.find("BlastOutput_param/Parameters")
    return {
        "program": root.findtext("BlastOutput_program"),
        "version": root.findtext("BlastOutput_version"),
        "db": root.findtext("BlastOutput_db"),
        "query_len": root.findtext("BlastOutput_query-len"),
        "eff_space": stats.findtext("Statistics_eff-space") if stats is not None else None,
        "db_num": stats.findtext("Statistics_db-num") if stats is not None else None,
        "db_len": stats.findtext("Statistics_db-len") if stats is not None else None,
        "hsp_len": stats.findtext("Statistics_hsp-len") if stats is not None else None,
        "filter": params.findtext("Parameters_filter") if params is not None else None,
        "reward": params.findtext("Parameters_sc-match") if params is not None else None,
        "penalty": params.findtext("Parameters_sc-mismatch") if params is not None else None,
        "gapopen": params.findtext("Parameters_gap-open") if params is not None else None,
        "gapextend": params.findtext("Parameters_gap-extend") if params is not None else None,
    }


def submit_to_blastalign(case: dict[str, str]) -> str:
    params = {
        "CMD": "Put",
        "PROGRAM": "blastn",
        "PAGE_TYPE": "BlastSearch",
        "QUERY": case["query"],
        "SUBJECTS": case["subjects"],
        "MEGABLAST": "off",
        "EXPECT": "1000",
        "FILTER": "F",
        "FORMAT_TYPE": "XML",
    }
    request = urllib.request.Request(
        "https://blast.ncbi.nlm.nih.gov/BlastAlign.cgi",
        data=urllib.parse.urlencode(params).encode(),
    )
    text = (
        urllib.request.urlopen(  # noqa: S310 - Fixed HTTPS endpoint for NCBI dev probe.
            request,
            timeout=60,
        )
        .read()
        .decode("utf-8", "replace")
    )
    match = re.search(r"RID\s*=\s*([A-Z0-9]+)", text)
    if not match:
        raise RuntimeError(f"RID not found for {case['name']}: {text[:500]}")
    return match.group(1)


def fetch_xml(rid: str) -> bytes:
    query = urllib.parse.urlencode({"CMD": "Get", "RID": rid, "FORMAT_TYPE": "XML"})
    xml_bytes = urllib.request.urlopen(
        "https://blast.ncbi.nlm.nih.gov/Blast.cgi?" + query,
        timeout=60,
    ).read()
    if b"<BlastOutput" not in xml_bytes:
        raise RuntimeError(f"RID {rid} did not return XML yet: {xml_bytes[:500]!r}")
    return xml_bytes


def run_local(
    case: dict[str, str], image: str, searchsp: int | None = None
) -> list[dict[str, object]]:
    tmp_path = Path(tempfile.mkdtemp(prefix=f"ncbi-searchsp-{case['name']}."))
    tmp_path.chmod(0o777)
    (tmp_path / "query.fa").write_text(case["query"])
    (tmp_path / "subjects.fa").write_text(case["subjects"])
    for path in tmp_path.iterdir():
        path.chmod(0o666)
    searchsp_arg = f" -searchsp {searchsp}" if searchsp is not None else ""
    command = f"""
set -euo pipefail
makeblastdb -in /work/subjects.fa -dbtype nucl -parse_seqids \
  -out /work/db >/work/makeblastdb.log 2>&1
blastn -query /work/query.fa -db /work/db {BASE_OPTIONS}{searchsp_arg} \
  -out /work/local.xml
""".strip()
    docker_path = shutil.which("docker")
    if docker_path is None:
        raise RuntimeError("docker executable not found")
    subprocess.run(  # noqa: S603 - Manual dev probe invokes local Docker.
        [
            docker_path,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/bash",
            "-v",
            f"{tmp_path}:/work",
            image,
            "-lc",
            command,
        ],
        check=True,
    )
    return parse_rows(tmp_path / "local.xml")


def infer_searchsp(
    web_rows: list[dict[str, object]],
    local_searchsp_1_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    local_by_name = {str(row["name"]): row for row in local_searchsp_1_rows}
    inferred = []
    for web_row in web_rows:
        name = str(web_row["name"])
        local_row = local_by_name.get(name)
        if local_row is None:
            continue
        inferred.append(
            {
                "name": name,
                "inferred_searchsp": float(web_row["evalue"]) / float(local_row["evalue"]),
                "web_evalue": web_row["evalue"],
                "local_searchsp_1_evalue": local_row["evalue"],
                "web_bitscore": web_row["bitscore"],
                "local_bitscore": local_row["bitscore"],
            }
        )
    return inferred


def rounded_searchsp(inferred: list[dict[str, object]]) -> int:
    return round(sum(float(row["inferred_searchsp"]) for row in inferred) / len(inferred))


def compact(rows: list[dict[str, object]]) -> list[tuple[str, float, float]]:
    return [(str(row["name"]), float(row["evalue"]), float(row["bitscore"])) for row in rows]


def run_case(case: dict[str, str], image: str) -> dict[str, Any]:
    rid = submit_to_blastalign(case)
    web_xml = fetch_xml(rid)
    web_rows = parse_rows(web_xml)
    local_default = run_local(case, image)
    local_searchsp_1 = run_local(case, image, 1)
    inferred = infer_searchsp(web_rows, local_searchsp_1)
    searchsp = rounded_searchsp(inferred)
    local_inferred = run_local(case, image, searchsp)
    return {
        "name": case["name"],
        "rid": rid,
        "web_stats": parse_stats(web_xml),
        "web_rows": web_rows,
        "local_default_rows": local_default,
        "local_searchsp_1_rows": local_searchsp_1,
        "inferred_by_hit": inferred,
        "rounded_searchsp": searchsp,
        "local_inferred_rows": local_inferred,
        "local_default_equals_web": compact(local_default) == compact(web_rows),
        "local_inferred_equals_web": compact(local_inferred) == compact(web_rows),
    }


def summarize(results: list[dict[str, Any]]) -> None:
    for result in results:
        inferred_values = [float(row["inferred_searchsp"]) for row in result["inferred_by_hit"]]
        print(
            result["name"],
            "rid=",
            result["rid"],
            "searchsp=",
            result["rounded_searchsp"],
            "range=",
            f"{min(inferred_values):.3f}..{max(inferred_values):.3f}",
            "default_equals_web=",
            result["local_default_equals_web"],
            "inferred_equals_web=",
            result["local_inferred_equals_web"],
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Infer NCBI Web BLASTAlign effective search space for small custom DB cases.")
    )
    parser.add_argument(
        "--image", default="elb-terminal:dev", help="Docker image containing BLAST+ tools."
    )
    parser.add_argument(
        "--output",
        default=str(Path(tempfile.gettempdir()) / "ncbi-searchsp-discovery.json"),
        help="JSON output path.",
    )
    args = parser.parse_args()

    results = [run_case(case, args.image) for case in CASES]
    Path(args.output).write_text(json.dumps(results, indent=2, sort_keys=True))
    summarize(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
