#!/usr/bin/env python3
"""Patch the sibling docker-openapi build context for dashboard runtime policy.

The sibling repository remains the source for the OpenAPI service, but this
dashboard currently needs two runtime guarantees that are not safe to leave to
the historical image defaults:

* the uvicorn Python environment must see the installed ``elastic_blast``
  package so it can create the ``elb-scripts`` ConfigMap;
* ``resource_profile=core_nt_precise`` must emit the local-SSD, 10-shard
  ``core_nt`` ElasticBLAST config used by the dashboard validation path.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def _insert_once(path: Path, anchor: str, insertion: str, marker: str) -> None:
    text = path.read_text()
    if marker in text:
        return
    count = text.count(anchor)
    if count != 1:
        raise RuntimeError(f"expected one anchor in {path}, found {count}")
    path.write_text(text.replace(anchor, anchor + insertion, 1))


def patch_dockerfile(root: Path) -> None:
    path = root / "Dockerfile"
    _replace_once(path, "ARG ELB_REF=dad3943\n", "ARG ELB_REF=7a471297\n")
    _insert_once(
        path,
        "COPY ./app /app\n",
        (
            "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py\n"
            "COPY merge-sharded-results.sh /tmp/merge-sharded-results.sh\n"
        ),
        "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py",
    )
    _insert_once(
        path,
        "    git -C /tmp/elb-src checkout ${ELB_REF} && \\\n",
        "    python3 /tmp/patch_elastic_blast.py /tmp/elb-src /tmp/merge-sharded-results.sh && \\\n",
        "python3 /tmp/patch_elastic_blast.py /tmp/elb-src",
    )
    _replace_once(
        path,
        "    rm -rf /tmp/elb-src && \\\n",
        "    true && \\\n",
    )
    _replace_once(
        path,
        "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n",
        (
            "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n"
            "    cp -a /tmp/elb-src/src/elastic_blast/templates/. /usr/local/lib/python3.11/site-packages/elastic_blast/templates/ && \\\n"
        ),
    )
    _replace_once(
        path,
        "    && pip install --no-cache-dir azure-cli \\\n",
        (
            "    && pip install --no-cache-dir azure-cli \\\n"
            "    && pip install --no-cache-dir --no-deps --no-build-isolation /tmp/elb-src \\\n"
            "    && cp -a /tmp/elb-src/src/elastic_blast/templates/. /opt/venv/lib/python3.11/site-packages/elastic_blast/templates/ \\\n"
            "    && rm -rf /tmp/elb-src \\\n"
        ),
    )


def patch_app(root: Path) -> None:
    path = root / "app" / "main.py"
    _insert_once(
        path,
        (
            "    config[\"cluster\"][\"num-nodes\"] = str(NUM_NODES)\n"
            "    config[\"blast\"][\"program\"] = req.program\n"
        ),
        (
            "    # Dashboard policy: OpenAPI submissions use AKS node-local SSD,\n"
            "    # not the historical shared PV/PVC path.\n"
            "    config[\"cluster\"][\"exp-use-local-ssd\"] = \"true\"\n"
            "    config[\"cluster\"][\"reuse\"] = \"true\"\n"
        ),
        "Dashboard policy: OpenAPI submissions use AKS node-local SSD",
    )
    _insert_once(
        path,
        "    if req.batch_len is not None:\n        config[\"blast\"][\"batch-len\"] = str(req.batch_len)\n",
        (
            "\n    db_name = _db_name_from_value(req.db)\n"
            "    profile = str(req.resource_profile or \"\").strip().lower()\n"
            "    if db_name == \"core_nt\" and profile in {\"core_nt_precise\", \"precise\", \"core_nt_safe\"}:\n"
            "        partitions = max(1, min(NUM_NODES, 10))\n"
            "        config[\"blast\"][\"db-partitions\"] = str(partitions)\n"
            "        config[\"blast\"][\"db-partition-prefix\"] = (\n"
            "            f\"{_blob_base()}/blast-db/{partitions}shards/core_nt_shard_\"\n"
            "        )\n"
            "        if \"-searchsp\" not in opts and \"-dbsize\" not in opts:\n"
            "            config[\"blast\"][\"options\"] = f\"{opts} -searchsp 32156241807668\"\n"
        ),
        "profile in {\"core_nt_precise\", \"precise\", \"core_nt_safe\"}",
    )
    text = path.read_text()
    duplicate = "    db_name = _db_name_from_value(req.db)\n    blast_version = _blast_version_detail()"
    if duplicate in text:
        path.write_text(text.replace(duplicate, "    blast_version = _blast_version_detail()", 1))


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-openapi-build-context.py /path/to/docker-openapi", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    if not (root / "Dockerfile").is_file() or not (root / "app" / "main.py").is_file():
        print(f"not a docker-openapi build context: {root}", file=sys.stderr)
        return 2
    patch_dockerfile(root)
    patch_app(root)
    print("patched docker-openapi build context for local SSD core_nt precise mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())