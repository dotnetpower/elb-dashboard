"""Kubernetes job builders for cached BLAST DB tie-order oracles.

Responsibility: Kubernetes job builders for cached BLAST DB tie-order oracles
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `DbOrderOracleJobPlan`, `oracle_status_blob_path`, `oracle_part_blob_path`,
`oracle_part_url`, `build_db_order_oracle_job_plan`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from api.services.warmup_jobs import (
    DEFAULT_CONTAINER_DB_PATH,
    DEFAULT_NAMESPACE,
    DEFAULT_NODE_DB_PATH,
)

ORACLE_PREFIX_ROOT = "metadata/oracles"
ORACLE_STATUS_BLOB_NAME = "status.json"
ORACLE_PARTS_DIR = "parts"

_SAFE_DB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_NODE_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
_SAFE_SHARD_RE = re.compile(r"^[0-9]{2}$")


@dataclass(frozen=True, slots=True)
class DbOrderOracleJobPlan:
    db_name: str
    storage_account: str
    run_id: str
    namespace: str
    jobs: tuple[dict[str, Any], ...]
    part_urls: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_name": self.db_name,
            "storage_account": self.storage_account,
            "run_id": self.run_id,
            "namespace": self.namespace,
            "jobs": list(self.jobs),
            "part_urls": list(self.part_urls),
        }


def oracle_status_blob_path(db_name: str) -> str:
    _validate_db_name(db_name)
    return f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_STATUS_BLOB_NAME}"


def oracle_part_blob_path(db_name: str, run_id: str, shard: str) -> str:
    _validate_db_name(db_name)
    _validate_run_id(run_id)
    if not _SAFE_SHARD_RE.match(shard):
        raise ValueError(f"invalid shard: {shard!r}")
    return f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_PARTS_DIR}/{run_id}/{shard}.txt"


def oracle_part_url(storage_account: str, db_name: str, run_id: str, shard: str) -> str:
    _validate_storage_account(storage_account)
    return (
        f"https://{storage_account}.blob.core.windows.net/blast-db/"
        f"{oracle_part_blob_path(db_name, run_id, shard)}"
    )


def build_db_order_oracle_job_plan(
    *,
    db_name: str,
    storage_account: str,
    run_id: str,
    shard_nodes: list[tuple[str, str] | tuple[str, str, str]],
    image: str,
    namespace: str = DEFAULT_NAMESPACE,
    node_db_path: str = DEFAULT_NODE_DB_PATH,
) -> DbOrderOracleJobPlan:
    """Build one Job per warmed shard to dump DB accession order.

    Each job runs on the node that already holds the warmed shard, emits the
    shard's BLAST DB accession order with ``blastdbcmd``, and uploads a text
    part to Storage. The submit path later passes the ordered part URLs to the
    finalizer; BLAST submissions do not regenerate this data.
    """

    _validate_db_name(db_name)
    _validate_storage_account(storage_account)
    _validate_run_id(run_id)
    if not _SAFE_IMAGE_RE.match(image):
        raise ValueError(f"invalid image: {image!r}")
    if not _SAFE_LABEL_RE.match(namespace):
        raise ValueError(f"invalid namespace: {namespace!r}")
    _validate_node_db_path(node_db_path)
    if not shard_nodes:
        raise ValueError("shard_nodes must not be empty")

    jobs: list[dict[str, Any]] = []
    part_urls: list[str] = []
    for shard_node in shard_nodes:
        shard, node_name, shard_node_db_path = _normalise_shard_node(
            shard_node,
            default_node_db_path=node_db_path,
        )
        if not _SAFE_SHARD_RE.match(shard):
            raise ValueError(f"invalid shard: {shard!r}")
        if not _SAFE_NODE_RE.match(node_name):
            raise ValueError(f"invalid node name: {node_name!r}")
        part_url = oracle_part_url(storage_account, db_name, run_id, shard)
        jobs.append(
            _build_job(
                db_name=db_name,
                storage_account=storage_account,
                run_id=run_id,
                shard=shard,
                node_name=node_name,
                image=image,
                namespace=namespace,
                node_db_path=shard_node_db_path,
                part_url=part_url,
            )
        )
        part_urls.append(part_url)
    return DbOrderOracleJobPlan(
        db_name=db_name,
        storage_account=storage_account,
        run_id=run_id,
        namespace=namespace,
        jobs=tuple(jobs),
        part_urls=tuple(part_urls),
    )


def _build_job(
    *,
    db_name: str,
    storage_account: str,
    run_id: str,
    shard: str,
    node_name: str,
    image: str,
    namespace: str,
    node_db_path: str,
    part_url: str,
) -> dict[str, Any]:
    job_name = _oracle_job_name(db_name, shard, run_id)
    shard_db = f"{db_name}_shard_{shard}"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "app": "elb-db-order-oracle",
                "db": _label_value(db_name),
                "shard": shard,
                "oracle-run": _label_value(run_id),
            },
        },
        "spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {
                    "labels": {
                        "app": "elb-db-order-oracle",
                        "db": _label_value(db_name),
                        "shard": shard,
                        "oracle-run": _label_value(run_id),
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "nodeName": node_name,
                    "tolerations": [
                        {
                            "key": "workload",
                            "operator": "Equal",
                            "value": "blast",
                            "effect": "NoSchedule",
                        }
                    ],
                    "containers": [
                        {
                            "name": "oracle",
                            "image": image,
                            "command": ["bash", "-lc"],
                            "args": [_oracle_shell_command()],
                            "env": [
                                {"name": "ELB_DB", "value": shard_db},
                                {"name": "ELB_DB_NAME", "value": db_name},
                                {"name": "ELB_SHARD", "value": shard},
                                {"name": "ELB_ORACLE_PART_URL", "value": part_url},
                                {"name": "ELB_STORAGE_ACCOUNT", "value": storage_account},
                            ],
                            "volumeMounts": [
                                {"name": "db", "mountPath": DEFAULT_CONTAINER_DB_PATH},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "db",
                            "hostPath": {
                                "path": node_db_path.rstrip("/"),
                                "type": "DirectoryOrCreate",
                            },
                        }
                    ],
                },
            },
        },
    }


def _normalise_shard_node(
    shard_node: tuple[str, str] | tuple[str, str, str],
    *,
    default_node_db_path: str,
) -> tuple[str, str, str]:
    if len(shard_node) == 2:
        shard, node_name = shard_node
        return shard, node_name, default_node_db_path
    if len(shard_node) == 3:
        shard, node_name, node_db_path = shard_node
        _validate_node_db_path(node_db_path)
        return shard, node_name, node_db_path
    raise ValueError("shard_nodes entries must be (shard, node) or (shard, node, node_db_path)")


def _validate_node_db_path(node_db_path: str) -> None:
    if not node_db_path.startswith("/") or ".." in node_db_path.split("/"):
        raise ValueError("node_db_path must be an absolute path without '..'")


def _oracle_shell_command() -> str:
    return r"""
set -euo pipefail
cd /blast/blastdb
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
out="/tmp/${ELB_DB_NAME}-${ELB_SHARD}-db-order-oracle.txt"
log "START db=${ELB_DB} shard=${ELB_SHARD} node=$(hostname)"
azcopy login --identity >/dev/null
blastdbcmd -db "${ELB_DB}" -entry all -outfmt '%a' \
  | awk 'NF && !seen[$1]++ { print $1 }' > "${out}"
count=$(wc -l < "${out}" | tr -d ' ')
if [ "${count}" = "0" ]; then
  log "ERROR no accessions emitted for ${ELB_DB}"
  exit 1
fi
azcopy cp "${out}" "${ELB_ORACLE_PART_URL}" --overwrite=true --log-level=ERROR
log "DONE db=${ELB_DB} shard=${ELB_SHARD} accessions=${count}"
""".strip()


def _validate_db_name(db_name: str) -> None:
    if not _SAFE_DB_RE.match(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")


def _validate_storage_account(storage_account: str) -> None:
    if not re.match(r"^[a-z0-9]{3,24}$", storage_account):
        raise ValueError(f"invalid storage_account: {storage_account!r}")


def _validate_run_id(run_id: str) -> None:
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,31}$", run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")


def _job_name_fragment(db_name: str) -> str:
    fragment = re.sub(r"[^a-z0-9-]+", "-", db_name.lower()).strip("-")
    return fragment[:36].strip("-") or "db"


def _oracle_job_name(db_name: str, shard: str, run_id: str) -> str:
    suffix = f"{shard}-{_job_name_fragment(run_id)}"
    db_budget = max(1, 63 - len("oracle") - 2 - len(suffix))
    db_fragment = _job_name_fragment(db_name)[:db_budget].strip("-") or "db"
    return f"oracle-{db_fragment}-{suffix}"


def _label_value(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    return label[:63].rstrip("-_.") or "value"
