"""Generate elastic-blast INI configuration from structured input."""

from __future__ import annotations

import configparser
import io
from typing import Any


def generate_config(params: dict[str, Any]) -> str:
    """Build an elastic-blast.ini from a flat dict of parameters.

    Returns the INI text suitable for writing to a file or uploading.
    """
    cfg = configparser.ConfigParser()

    # [cloud-provider]
    cfg.add_section("cloud-provider")
    cfg.set("cloud-provider", "azure-region", params.get("region", "koreacentral"))
    if params.get("acr_resource_group"):
        cfg.set("cloud-provider", "azure-acr-resource-group", params["acr_resource_group"])
    if params.get("acr_name"):
        cfg.set("cloud-provider", "azure-acr-name", params["acr_name"])
    cfg.set("cloud-provider", "azure-resource-group", params.get("resource_group", ""))
    if params.get("storage_account"):
        cfg.set("cloud-provider", "azure-storage-account", params["storage_account"])

    # Derive storage container from db path (e.g. "blast-db/16S_ribosomal_RNA/...")
    db_raw = params.get("db", "")
    if db_raw and not db_raw.startswith("http"):
        container_name = db_raw.split("/")[0]
    elif db_raw.startswith("https://"):
        # https://account.blob.core.windows.net/container/...
        parts = db_raw.split("/", 4)
        container_name = parts[3] if len(parts) > 3 else "blast-db"
    else:
        container_name = "blast-db"
    cfg.set("cloud-provider", "azure-storage-account-container", container_name)

    # [cluster]
    cfg.add_section("cluster")
    job_id = params.get("job_id", "blast-job")
    # Use existing AKS cluster name if provided, otherwise generate per-job name
    aks_cluster_name = params.get("aks_cluster_name", "")
    if aks_cluster_name:
        cfg.set("cluster", "name", aks_cluster_name)
    else:
        cfg.set("cluster", "name", f"elastic-blast-{job_id[:12]}")
    cfg.set("cluster", "machine-type", params.get("machine_type", "Standard_D8s_v3"))
    cfg.set("cluster", "num-nodes", str(params.get("num_nodes", 1)))
    cfg.set("cluster", "pd-size", params.get("pd_size", "3000Gi"))

    # Local SSD for DB sharding (warmup mode)
    if params.get("enable_warmup") or params.get("use_local_ssd"):
        cfg.set("cluster", "exp-use-local-ssd", "true")

    # Warm cluster reuse mode
    if params.get("reuse"):
        cfg.set("cluster", "reuse", "true")

    # [blast]
    cfg.add_section("blast")
    cfg.set("blast", "program", params.get("program", "blastn"))
    cfg.set("blast", "db", params.get("db", ""))
    cfg.set("blast", "queries", params.get("query_blob_url", ""))
    cfg.set("blast", "results", params.get("results_url", ""))

    # DB partitioning / sharding
    if params.get("db_auto_partition"):
        cfg.set("blast", "db-auto-partition", "true")
    db_partitions = params.get("db_partitions")
    if db_partitions and int(db_partitions) > 0:
        cfg.set("blast", "db-partitions", str(db_partitions))
    db_partition_prefix = params.get("db_partition_prefix")
    if db_partition_prefix:
        cfg.set("blast", "db-partition-prefix", db_partition_prefix)

    # Build options string
    options_parts: list[str] = []
    evalue = params.get("evalue")
    if evalue is not None:
        options_parts.append(f"-evalue {evalue}")
    max_target_seqs = params.get("max_target_seqs")
    if max_target_seqs is not None:
        options_parts.append(f"-max_target_seqs {max_target_seqs}")
    outfmt = params.get("outfmt")
    if outfmt is not None:
        options_parts.append(f"-outfmt {outfmt}")
    word_size = params.get("word_size")
    if word_size is not None:
        options_parts.append(f"-word_size {word_size}")
    gap_open = params.get("gap_open")
    if gap_open is not None:
        options_parts.append(f"-gapopen {gap_open}")
    gap_extend = params.get("gap_extend")
    if gap_extend is not None:
        options_parts.append(f"-gapextend {gap_extend}")
    additional = params.get("additional_options", "").strip()
    if additional:
        # Reject shell metacharacters to prevent command injection
        import re
        _SHELL_META = re.compile(r"[;&|`$(){}\\!\n\r]")
        if _SHELL_META.search(additional):
            raise ValueError(f"additional_options contains forbidden characters: {additional[:50]}")
        options_parts.append(additional)
    if options_parts:
        cfg.set("blast", "options", " ".join(options_parts))

    mem_request = params.get("mem_request")
    if mem_request:
        cfg.set("blast", "mem-request", mem_request)
    mem_limit = params.get("mem_limit")
    if mem_limit:
        cfg.set("blast", "mem-limit", mem_limit)
    batch_len = params.get("batch_len")
    if batch_len:
        cfg.set("blast", "batch-len", str(batch_len))

    # [timeouts]
    cfg.add_section("timeouts")
    cfg.set("timeouts", "init-pv", "45")
    cfg.set("timeouts", "blast-k8s-job", "10080")

    buf = io.StringIO()
    cfg.write(buf)
    return buf.getvalue()
