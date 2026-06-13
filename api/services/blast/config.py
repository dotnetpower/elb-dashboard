"""Generate elastic-blast INI configuration from structured input.

Responsibility: Generate elastic-blast INI configuration from structured input
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `generate_config`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import configparser
import io
from typing import Any

from api.services.aks_skus import (
    AZURE_VM_HOURLY_USD as _ALLOWED_PRICES,
)
from api.services.aks_skus import (
    DEFAULT_SKU,
)
from api.services.sharding_precision import (
    build_precision_report,
    normalize_sharding_mode,
    option_value,
    outfmt_is_merge_compatible,
    uniform_query_effective_search_space,
)
from api.services.storage.url_validation import validate_storage_blob_reference


def generate_config(params: dict[str, Any]) -> str:
    """Build an elastic-blast.ini from a flat dict of parameters.

    Returns the INI text suitable for writing to a file or uploading.
    """
    cfg = configparser.ConfigParser()

    def _positive_int_option(key: str) -> int | None:
        raw = params.get(key)
        if raw in (None, ""):
            return None
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a positive integer") from exc
        if value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        return value

    def _taxonomy_filter_option() -> tuple[str, int] | None:
        if isinstance(params.get("taxid"), bool):
            raise ValueError("taxid must be a positive integer")
        taxid = _positive_int_option("taxid")
        if taxid is None:
            return None
        is_inclusive = params.get("is_inclusive")
        if is_inclusive is None:
            inclusive = True
        elif isinstance(is_inclusive, bool):
            inclusive = is_inclusive
        elif isinstance(is_inclusive, str):
            lowered = is_inclusive.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                inclusive = True
            elif lowered in {"0", "false", "no", "off"}:
                inclusive = False
            else:
                raise ValueError("is_inclusive must be a boolean")
        else:
            raise ValueError("is_inclusive must be a boolean")
        return ("-taxids" if inclusive else "-negative_taxids", taxid)

    def _bool_option(key: str) -> bool | None:
        raw = params.get(key)
        if raw in (None, ""):
            return None
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        raise ValueError(f"{key} must be a boolean")

    # [cloud-provider]
    cfg.add_section("cloud-provider")
    cfg.set("cloud-provider", "azure-region", params.get("region", "koreacentral"))
    acr_rg = params.get("acr_resource_group", "")
    if acr_rg:
        cfg.set("cloud-provider", "azure-acr-resource-group", acr_rg)
    acr_name = params.get("acr_name", "")
    if acr_name:
        cfg.set("cloud-provider", "azure-acr-name", acr_name)
    cfg.set("cloud-provider", "azure-resource-group", params.get("resource_group", ""))
    storage_account = str(params.get("storage_account") or "")
    if storage_account:
        validate_storage_blob_reference(
            storage_account=storage_account,
            value=params.get("db"),
            label="db",
            expected_container="blast-db",
        )
        validate_storage_blob_reference(
            storage_account=storage_account,
            value=params.get("query_blob_url"),
            label="query_blob_url",
            expected_container="queries",
        )
        validate_storage_blob_reference(
            storage_account=storage_account,
            value=params.get("results_url"),
            label="results_url",
            expected_container="results",
            require_blob_path=False,
        )
        cfg.set("cloud-provider", "azure-storage-account", storage_account)

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
    cfg.set("cluster", "machine-type", params.get("machine_type", DEFAULT_SKU))
    cfg.set("cluster", "num-nodes", str(params.get("num_nodes", 1)))
    cfg.set("cluster", "pd-size", params.get("pd_size", "3000Gi"))

    # The dashboard currently runs ElasticBLAST only through the AKS node-local
    # SSD path. Even if an older client submits ``use_local_ssd=false``, ignore
    # it for now so upstream ElasticBLAST cannot fall back to its shared PV/PVC
    # init path before that mode is deliberately productized here.
    cfg.set("cluster", "exp-use-local-ssd", "true")

    if params.get("skip_warmed_ssd_init"):
        cfg.set("cluster", "exp-skip-warmed-ssd-init", "true")

    # Warm cluster reuse mode
    if params.get("reuse"):
        cfg.set("cluster", "reuse", "true")

    # [blast]
    cfg.add_section("blast")
    cfg.set("blast", "program", params.get("program", "blastn"))
    cfg.set("blast", "db", params.get("db", ""))
    cfg.set("blast", "queries", params.get("query_blob_url", ""))
    cfg.set("blast", "results", params.get("results_url", ""))

    sharding_mode = normalize_sharding_mode(params)

    # DB partitioning / sharding. ``db_auto_partition`` is a control-plane hint
    # only: the Azure runtime used by this dashboard expects explicit
    # db-partitions/db-partition-prefix values when sharding is selected.
    if params.get("db_auto_partition"):
        if sharding_mode == "off":
            raise ValueError(
                "db_auto_partition requires sharding_mode=approximate or precise; "
                "partitioned BLAST output is not guaranteed to match full-DB BLAST"
            )
    db_partitions = params.get("db_partitions")
    if db_partitions and int(db_partitions) > 0:
        cfg.set("blast", "db-partitions", str(db_partitions))
    db_partition_prefix = params.get("db_partition_prefix")
    if db_partition_prefix:
        cfg.set("blast", "db-partition-prefix", db_partition_prefix)

    # Experimental DB sharding for warmed DBs.
    #
    # Correctness policy (2026-05): sharded BLAST output is not guaranteed
    # to be byte/row-equivalent to a full-DB BLAST+ or NCBI Web BLAST run.
    # Shard-local e-value statistics, max_target_seqs pruning, output-format
    # specific merge semantics, and tie-breaking all need an explicit
    # equivalence gate before this can be a default submit path. Keep the
    # automatic injection behind an intentionally loud opt-in flag so normal
    # dashboard submits preserve full-DB semantics.
    user_set_partitions = bool(params.get("db_partitions"))
    user_set_prefix = bool(params.get("db_partition_prefix"))
    auto_shard_eligible = (
        sharding_mode != "off"
        and not params.get("disable_sharding")
        and not user_set_partitions
        and not user_set_prefix
        and bool(params.get("db_sharded"))
        and bool(params.get("db_total_bytes"))
        and bool(params.get("db_name"))
        and bool(params.get("storage_account"))
    )
    if auto_shard_eligible:
        # Local import keeps the module free of an unconditional storage
        # dependency at import time (blast_config is also used in unit
        # tests that don't have azure-storage-blob installed by default
        # in some restricted environments).
        from api.services.db.sharding import (
            partition_prefix_for,
            select_partitions_for_submit,
        )

        n = select_partitions_for_submit(
            db_total_bytes=int(params["db_total_bytes"]),
            num_nodes=int(params.get("num_nodes", 1)),
            machine_type=params.get("machine_type", DEFAULT_SKU),
        )
        # The current Azure local-SSD shard template pins shard N to node
        # ordinal N. If the memory floor asks for more shards than nodes,
        # jobs for the missing ordinals would never schedule. Refuse instead
        # of silently producing a broken ElasticBLAST config.
        num_nodes = int(params.get("num_nodes", 1))
        if n > num_nodes:
            raise ValueError(
                "approximate sharding requires at least one node per shard; "
                f"selected {n} shards for {num_nodes} nodes"
            )
        cfg.set("blast", "db-partitions", str(n))
        cfg.set(
            "blast",
            "db-partition-prefix",
            partition_prefix_for(
                account_name=str(params["storage_account"]),
                db_name=str(params["db_name"]),
                num_shards=n,
                container=container_name,
            ),
        )
        # Sharding requires the local-SSD init script
        # (init-db-shard-aks.sh). Force it on even if the caller did not
        # request warmup mode — the alternative (init-db-partitioned-aks.sh)
        # cannot consume our manifest+.nal layout.
        cfg.set("cluster", "exp-use-local-ssd", "true")

    # Build options string
    options_parts: list[str] = []
    additional = params.get("additional_options", "").strip()

    def _additional_has_blast_option(option: str) -> bool:
        import re as _re_option

        return bool(_re_option.search(rf"(?<!\S){_re_option.escape(option)}(?:\s|=|$)", additional))

    evalue = params.get("evalue")
    if evalue is not None:
        options_parts.append(f"-evalue {evalue}")
    max_target_seqs = params.get("max_target_seqs")
    if max_target_seqs is not None:
        options_parts.append(f"-max_target_seqs {max_target_seqs}")
    taxonomy_filter = _taxonomy_filter_option()
    if taxonomy_filter is not None:
        taxonomy_option, taxonomy_id = taxonomy_filter
        if _additional_has_blast_option("-taxids") or _additional_has_blast_option(
            "-negative_taxids"
        ):
            raise ValueError(
                "taxid conflicts with taxonomy filters already present in additional_options"
            )
        options_parts.append(f"{taxonomy_option} {taxonomy_id}")
    outfmt = params.get("outfmt")
    outfmt_str: str | None = None
    if outfmt is not None:
        # outfmt is rendered into the generated K8s Job YAML by elastic-blast.
        # Quotes / shell metas inside the value break that YAML (kubectl apply
        # → "did not find expected key"). Reject them at the boundary so the
        # error surfaces here, not 60 s later in the cluster.
        outfmt_str = str(outfmt).strip()
        import re as _re_outfmt

        if _re_outfmt.search(r'["\'`;&|$(){}\\\n\r]', outfmt_str):
            raise ValueError(f"outfmt contains forbidden characters: {outfmt_str[:50]}")
        # The `outfmt` field cannot carry a multi-token format specifier (e.g.
        # `7 std staxids ...`). A multi-token value here would be emitted into
        # the ini and elastic-blast's shlex.split would then hand `std`,
        # `staxids`, … to BLAST as stray positional args — a silent failure that
        # only surfaces ~60 s later in the cluster. Reject it at the boundary
        # with an actionable message pointing at `additional_options`.
        #
        # The canonical wire format for an extended layout is the UNQUOTED
        # multi-token specifier: `-outfmt 7 std staxids sscinames`. Do NOT quote
        # it — the sibling runtime injects ELB_BLAST_OPTIONS into the K8s Job
        # YAML via a raw `${VAR}` regex substitution with no YAML escaping
        # (`value: "${ELB_BLAST_OPTIONS}"`), so a quoted value
        # (`-outfmt "7 std staxids"`) produces `value: "-outfmt "7 std staxids""`
        # and breaks `kubectl apply`. The deployed `blast-run-aks.sh` rebuilds
        # the argv and rejoins the `-outfmt` tokens up to the next `-flag` into a
        # single blastn argument, so the unquoted form survives the shell
        # word-split. Lead the layout with `std` (or `qseqid`) because the
        # read-side merge groups by qseqid.
        if " " in outfmt_str:
            raise ValueError(
                "outfmt only accepts a single format code here (e.g. 5, 6, 7). "
                "For an extended tabular layout, pass it via additional_options "
                "UNQUOTED as -outfmt 7 std staxids sscinames (do not add quotes; "
                "quotes break the generated Job YAML). Lead with std so the "
                "qseqid column stays first."
            )
        options_parts.append(f"-outfmt {outfmt_str}")

    if cfg.has_option("blast", "db-partitions") or cfg.has_option("blast", "db-auto-partition"):
        additional_outfmt = option_value(additional, "-outfmt") if additional else None
        merge_outfmt = additional_outfmt if additional_outfmt is not None else outfmt_str
        if not outfmt_is_merge_compatible(merge_outfmt):
            raise ValueError(
                "sharded BLAST result merge currently supports only outfmt 5, "
                "outfmt 6, outfmt 7, or outfmt '6 std...'/'7 std...'"
            )

        report = build_precision_report(
            params,
            query_count=params.get("query_count"),
            db_stats_available=bool(params.get("db_total_letters")),
        )
        if sharding_mode == "precise" and not report.eligible:
            raise ValueError("; ".join(report.blocking_errors))
        if sharding_mode == "precise" and report.precision_level in {
            "precise_tabular_split",
            "precise_xml_split",
        }:
            raise ValueError("query-group split is required for mixed query search spaces")
    word_size = params.get("word_size")
    if word_size is not None:
        options_parts.append(f"-word_size {word_size}")
    low_complexity_filter = _bool_option("low_complexity_filter")
    if (
        low_complexity_filter is not None
        and str(params.get("program", "blastn")).strip() == "blastn"
        and not _additional_has_blast_option("-dust")
    ):
        options_parts.append(f"-dust {'yes' if low_complexity_filter else 'no'}")
    if (
        low_complexity_filter is True
        and str(params.get("program", "blastn")).strip() == "blastn"
        and not _additional_has_blast_option("-soft_masking")
    ):
        options_parts.append("-soft_masking false")
    gap_open = params.get("gap_open")
    if gap_open is not None:
        options_parts.append(f"-gapopen {gap_open}")
    gap_extend = params.get("gap_extend")
    if gap_extend is not None:
        options_parts.append(f"-gapextend {gap_extend}")

    effective_search_space = _positive_int_option("db_effective_search_space")
    if effective_search_space is None:
        effective_search_space = uniform_query_effective_search_space(
            params,
            _positive_int_option("query_count"),
        )
    if effective_search_space and not _additional_has_blast_option("-searchsp"):
        options_parts.append(f"-searchsp {effective_search_space}")
    elif (
        cfg.has_option("blast", "db-partitions")
        and not _additional_has_blast_option("-searchsp")
        and not _additional_has_blast_option("-dbsize")
    ):
        db_total_letters = _positive_int_option("db_total_letters")
        if db_total_letters:
            options_parts.append(f"-dbsize {db_total_letters}")

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


# Azure VM hourly cost estimates (Pay-As-You-Go, koreacentral).
#
# Re-exported from ``api.services.aks_skus`` so there is exactly one place
# in the codebase that defines the elastic-blast SKU allow-list and its
# pricing. Adding a SKU here without adding it to ``aks_skus.ALLOWED_SKUS``
# would be a footgun: the cost estimator would price something that the
# elastic-blast CLI then rejects in the cluster.
AZURE_VM_HOURLY_USD: dict[str, float] = dict(_ALLOWED_PRICES)
STORAGE_GB_MONTH_USD = 0.018  # Hot tier
PD_GB_MONTH_USD = 0.040  # Managed SSD
