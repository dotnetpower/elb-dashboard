"""DB sharding helpers - generate manifest + .nal alias text files.

Responsibility: DB sharding helpers - generate manifest + .nal alias text files
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `ShardLayout`, `_validate_db_name`, `_validate_shard_count`,
`list_db_volumes`, `read_blastdb_stats`, `derive_volumes_from_keys`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

from api.services.aks_skus import SKU_BY_NAME
from api.services.storage_data import _blob_service

LOGGER = logging.getLogger(__name__)

# AKS pod mount path where ``init-db-shard-aks.sh`` materialises volume
# files at runtime. Hardcoded in the sibling init script; do not configure
# from env unless the upstream script changes.
AKS_LOCAL_DB_DIR = "/blast/blastdb"

# Shard counts pre-created during warmup. Covers E16 x {3..10} default
# cluster sizes plus a single-shard "off" layout for small DBs / debugging.
PRESET_SHARD_SETS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 8, 10)

# Hardcap on N at submit-time (prevent absurd custom values from a malicious
# or buggy caller).
MAX_SHARDS = 32

# Memory headroom rule from v3 benchmark: shard size must fit in roughly
# half of node RAM to avoid page-cache eviction during the BLAST scan.
# v3 measured E16s_v5 (128 GB) scanning 27 GB shards at 21 % memory
# utilisation — comfortable. Pushing past 50 % triggers super-linear
# slowdown.
SAFE_SHARD_FRACTION_OF_NODE_RAM = 0.5

# Validation: db_name must be a tame identifier. ElasticBLAST itself uses
# alnum + ``._-`` only (see sibling ``util.py``). Restrict to the same
# alphabet here so a hostile input cannot escape into a blob path.
_RE_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")

# Maximum volumes per DB we are willing to enumerate before bailing out.
# core_nt has 83 volumes; 1024 is a safety net, not an expected limit.
_MAX_VOLUMES = 1024

DEFAULT_CONTAINER = "blast-db"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ShardLayout:
    """One concrete N-shard layout for a database."""

    db_name: str
    num_shards: int
    # ``shards[i]`` is the list of volume base names assigned to shard i.
    shards: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if self.num_shards != len(self.shards):
            raise ValueError(f"num_shards={self.num_shards} but len(shards)={len(self.shards)}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_db_name(db_name: str) -> None:
    if not _RE_DB_NAME.match(db_name or ""):
        raise ValueError(f"invalid db_name {db_name!r} — must match {_RE_DB_NAME.pattern}")


def _validate_shard_count(n: int) -> None:
    if not isinstance(n, int):
        raise TypeError(f"shard count must be int, got {type(n).__name__}")
    if n < 1 or n > MAX_SHARDS:
        raise ValueError(f"shard count {n} out of range [1, {MAX_SHARDS}]")


# ---------------------------------------------------------------------------
# Volume discovery
# ---------------------------------------------------------------------------
def list_db_volumes(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
) -> tuple[list[str], int]:
    """Return ``(sorted volume base names, total bytes across volumes)``.

    A "volume" is a numbered split (e.g. ``core_nt.00``, ``core_nt.01``).
    Single-volume databases (e.g. ``16S_ribosomal_RNA``) return one entry
    equal to ``db_name`` itself.

    Raises ``LookupError`` if no BLAST volume files are found under
    ``container/db_name/``.

    Volume detection uses ``.nsq`` (nucleotide) / ``.psq`` (protein) as the
    marker — every BLAST volume always has exactly one of these. We
    intentionally do **not** treat ``.nal``/``.pal`` alias files as
    volumes; for a single-volume DB shipped only as an alias, this returns
    a single entry equal to ``db_name`` once we sum bytes across all of
    its files.
    """
    _validate_db_name(db_name)
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    prefix = f"{db_name}/"
    # First pass: discover volumes by their unique sequence file.
    volume_size: dict[str, int] = {}
    # Track every blob's size so we can report total DB bytes (the marker
    # file alone is only a fraction of the volume's total bytes).
    blob_sizes: dict[str, int] = {}
    marker_re = re.compile(rf"^{re.escape(db_name)}(?:\.(\d+))?\.(?:nsq|psq)$")
    for blob in cc.list_blobs(name_starts_with=prefix):
        leaf = blob.name[len(prefix) :]
        if "/" in leaf:
            # Skip nested staging artefacts — volumes always live at the
            # immediate level under ``{db}/``.
            continue
        blob_sizes[leaf] = blob.size or 0
        m = marker_re.match(leaf)
        if not m:
            continue
        base = f"{db_name}.{m.group(1)}" if m.group(1) is not None else db_name
        volume_size[base] = 0  # placeholder; sum below
        if len(volume_size) > _MAX_VOLUMES:
            raise RuntimeError(
                f"db {db_name!r} has > {_MAX_VOLUMES} volumes; refusing to enumerate"
            )

    if not volume_size:
        raise LookupError(f"no BLAST volume files under {container}/{db_name}/ in {account_name!r}")

    # Second pass: attribute each blob's bytes to its volume by filename
    # prefix. ``core_nt.00.nhr``, ``core_nt.00.nin``, ``core_nt.00.nsq`` →
    # all summed under ``core_nt.00``.
    for leaf, size in blob_sizes.items():
        # Try multi-volume match first.
        m = re.match(rf"^({re.escape(db_name)}\.\d+)\.[a-z]{{2,4}}$", leaf)
        if m and m.group(1) in volume_size:
            volume_size[m.group(1)] += size
            continue
        # Single-volume match.
        if db_name in volume_size and re.match(rf"^{re.escape(db_name)}\.[a-z]{{2,4}}$", leaf):
            volume_size[db_name] += size

    volumes = sorted(volume_size.keys(), key=_volume_sort_key)
    total_bytes = sum(volume_size.values())
    return volumes, total_bytes


def read_blastdb_stats(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
) -> dict[str, int]:
    """Read full-DB statistics from the BLAST v5 ``.njs`` metadata file."""
    _validate_db_name(db_name)
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    bc = cc.get_blob_client(f"{db_name}/{db_name}.njs")
    try:
        from api.services.storage_data import read_metadata_blob_bytes

        raw = read_metadata_blob_bytes(bc, label="blast-db-njs")
    except ResourceNotFoundError:
        return {}
    except ValueError:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}

    stats: dict[str, int] = {}
    for source, target in (
        ("number-of-letters", "total_letters"),
        ("number-of-sequences", "total_sequences"),
        ("bytes-to-cache", "bytes_to_cache"),
        ("bytes-total", "bytes_total"),
    ):
        value = data.get(source)
        if isinstance(value, (int, float)) and value > 0:
            stats[target] = int(value)
    return stats


def _volume_sort_key(name: str) -> tuple[str, int]:
    """Sort ``foo.10`` AFTER ``foo.9`` (numeric, not lexical)."""
    m = re.match(r"^(.*)\.(\d+)$", name)
    if m:
        return (m.group(1), int(m.group(2)))
    return (name, -1)


def derive_volumes_from_keys(db_name: str, keys: Iterable[str]) -> list[str]:
    """Derive sorted unique volume base names from a list of file keys.

    A "key" is an object name like ``core_nt.00.nhr`` or
    ``core_nt/core_nt.00.nhr`` (with or without a directory prefix).

    Used by ``prepare-db`` to compute shard layouts the moment NCBI key
    enumeration finishes — *before* any blob copy completes — so the
    pipeline does not need a wait-for-copy poll loop. Detection uses the
    ``.nsq``/``.psq`` marker files for the same reason as
    :func:`list_db_volumes` (alias files are never volumes).
    """
    _validate_db_name(db_name)
    marker_re = re.compile(rf"^{re.escape(db_name)}(?:\.(\d+))?\.(?:nsq|psq)$")
    seen: set[str] = set()
    for raw in keys:
        leaf = raw.rsplit("/", 1)[-1]
        m = marker_re.match(leaf)
        if not m:
            continue
        base = f"{db_name}.{m.group(1)}" if m.group(1) is not None else db_name
        seen.add(base)
        if len(seen) > _MAX_VOLUMES:
            raise RuntimeError(
                f"db {db_name!r} has > {_MAX_VOLUMES} volumes; refusing to enumerate"
            )
    return sorted(seen, key=_volume_sort_key)


# ---------------------------------------------------------------------------
# Layout planning (contiguous block assignment)
# ---------------------------------------------------------------------------
def plan_shard_layout(db_name: str, volumes: list[str], num_shards: int) -> ShardLayout:
    """Distribute ``volumes`` across ``num_shards`` as contiguous blocks.

    Mirrors the sibling v3 ``benchmark/strategies/db_prep.py:shard_db()``
    contiguous-block assignment so result correctness (validated against
    full-DB reference in v3 report §3) carries over.

    If ``num_shards > len(volumes)``, the trailing shards are empty — but
    this is rejected to keep the AKS init script from downloading nothing.
    """
    _validate_db_name(db_name)
    _validate_shard_count(num_shards)
    if not volumes:
        raise ValueError("volumes is empty; cannot plan a shard layout")
    if num_shards > len(volumes):
        raise ValueError(
            f"num_shards={num_shards} exceeds volume count {len(volumes)} "
            f"for db {db_name!r}; trailing shards would be empty"
        )

    n = len(volumes)
    # Ceiling-divide so shard 0..k-1 have ``ceil(n/N)`` and the tail absorbs
    # the remainder (matches sibling v3 behaviour).
    block = (n + num_shards - 1) // num_shards
    shards: list[tuple[str, ...]] = []
    for i in range(num_shards):
        start = i * block
        end = min(start + block, n)
        shards.append(tuple(volumes[start:end]))
    return ShardLayout(db_name=db_name, num_shards=num_shards, shards=tuple(shards))


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------
def render_manifest(volumes: Iterable[str]) -> str:
    """Render the manifest text file (one volume base name per line)."""
    lines = list(volumes)
    if not lines:
        raise ValueError("cannot render an empty manifest")
    return "\n".join(lines) + "\n"


def render_nal(
    db_name: str,
    shard_idx: int,
    num_shards: int,
    volumes: Iterable[str],
    *,
    local_db_dir: str = AKS_LOCAL_DB_DIR,
) -> str:
    """Render a BLAST+ ``.nal`` alias file targeting ``local_db_dir``.

    Format matches what sibling ``init-db-shard-aks.sh`` rewrites at AKS
    runtime — ``TITLE`` then ``DBLIST`` with absolute volume paths.
    """
    _validate_db_name(db_name)
    _validate_shard_count(num_shards)
    if shard_idx < 0 or shard_idx >= num_shards:
        raise ValueError(f"shard_idx {shard_idx} out of range [0, {num_shards})")
    vol_list = list(volumes)
    if not vol_list:
        raise ValueError(f"shard {shard_idx} has no volumes; cannot render .nal")
    shard_name = f"{db_name}_shard_{shard_idx:02d}"
    paths = " ".join(f"{local_db_dir}/{v}" for v in vol_list)
    return f"TITLE {shard_name}\nDBLIST {paths}\n"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
@dataclass
class ShardUploadResult:
    db_name: str
    num_shards: int
    created: int
    skipped: int
    error: str | None = None


def _shard_blob_paths(db_name: str, num_shards: int, shard_idx: int) -> tuple[str, str]:
    shard_name = f"{db_name}_shard_{shard_idx:02d}"
    base = f"{num_shards}shards/{shard_name}"
    return f"{base}/{shard_name}.manifest", f"{base}/{shard_name}.nal"


def _shard_set_already_present(cc: Any, db_name: str, num_shards: int) -> bool:
    """Idempotency probe: shard set is "present" iff EVERY .nal blob exists.

    Checking only shard 00 would produce false positives if a previous run
    failed midway. Checking every shard catches partial uploads.
    """
    for i in range(num_shards):
        _, nal_path = _shard_blob_paths(db_name, num_shards, i)
        bc = cc.get_blob_client(nal_path)
        try:
            bc.get_blob_properties()
        except ResourceNotFoundError:
            return False
    return True


def upload_shard_set(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    num_shards: int,
    volumes: list[str],
    *,
    container: str = DEFAULT_CONTAINER,
    force: bool = False,
    local_db_dir: str = AKS_LOCAL_DB_DIR,
) -> ShardUploadResult:
    """Idempotently upload manifest + .nal for one ``N``-shard layout.

    If every ``.nal`` already exists and ``force`` is false, returns with
    ``skipped == 2*num_shards`` and no writes.
    """
    _validate_db_name(db_name)
    _validate_shard_count(num_shards)
    layout = plan_shard_layout(db_name, volumes, num_shards)

    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)

    if not force and _shard_set_already_present(cc, db_name, num_shards):
        return ShardUploadResult(
            db_name=db_name,
            num_shards=num_shards,
            created=0,
            skipped=2 * num_shards,
        )

    created = 0
    skipped = 0
    for i, shard_volumes in enumerate(layout.shards):
        manifest_path, nal_path = _shard_blob_paths(db_name, num_shards, i)
        manifest_text = render_manifest(shard_volumes)
        nal_text = render_nal(
            db_name=db_name,
            shard_idx=i,
            num_shards=num_shards,
            volumes=shard_volumes,
            local_db_dir=local_db_dir,
        )
        for path, text in ((manifest_path, manifest_text), (nal_path, nal_text)):
            bc = cc.get_blob_client(path)
            if not force:
                try:
                    # Manifest + .nal blobs are tiny (volume names only);
                    # cap the comparison read at 64 KiB so a corrupt
                    # oversized blob cannot OOM the worker.
                    from api.services.storage_data import read_metadata_blob_text

                    existing = read_metadata_blob_text(
                        bc, max_bytes=64 * 1024, label="shard-manifest"
                    )
                    if existing == text:
                        skipped += 1
                        continue
                except ResourceNotFoundError:
                    pass
                except ValueError:
                    # Over-cap means it's not our shape — overwrite.
                    pass
            bc.upload_blob(text.encode("utf-8"), overwrite=True)
            created += 1

    return ShardUploadResult(
        db_name=db_name,
        num_shards=num_shards,
        created=created,
        skipped=skipped,
    )


def ensure_shard_sets(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    presets: Iterable[int] = PRESET_SHARD_SETS,
    container: str = DEFAULT_CONTAINER,
) -> dict[str, Any]:
    """Ensure every preset N has a shard layout uploaded for ``db_name``.

    Returns a summary dict::

        {
            "db_name": "core_nt",
            "total_volumes": 83,
            "total_bytes": 269000000000,
            "shard_sets": [1, 2, 3, 4, 5, 6, 8, 10],
            "created": 12,
            "skipped": 4,
            "errors": [],
        }
    """
    _validate_db_name(db_name)
    volumes, total_bytes = list_db_volumes(credential, account_name, db_name, container=container)
    stats = read_blastdb_stats(credential, account_name, db_name, container=container)

    successful: list[int] = []
    created = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    for n in sorted(set(int(p) for p in presets)):
        if n > len(volumes):
            # Skip presets that exceed the volume count instead of failing
            # the whole batch — small DBs (e.g. 16S, 1 volume) just won't
            # have N=2..10 layouts. This is fine.
            LOGGER.info(
                "db_sharding: db=%s skipping N=%d (only %d volumes)",
                db_name,
                n,
                len(volumes),
            )
            continue
        try:
            result = upload_shard_set(
                credential,
                account_name,
                db_name,
                n,
                volumes,
                container=container,
            )
            successful.append(n)
            created += result.created
            skipped += result.skipped
        except Exception as exc:
            LOGGER.warning(
                "db_sharding: db=%s N=%d failed: %s",
                db_name,
                n,
                str(exc)[:200],
            )
            errors.append({"num_shards": n, "error": str(exc)[:200]})

    return {
        "db_name": db_name,
        "total_volumes": len(volumes),
        "total_bytes": total_bytes,
        **stats,
        "shard_sets": successful,
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Status / read
# ---------------------------------------------------------------------------
def shard_sets_present(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
    presets: Iterable[int] = PRESET_SHARD_SETS,
) -> list[int]:
    """Return the subset of ``presets`` for which every shard's .nal exists."""
    _validate_db_name(db_name)
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    ready = []
    for n in sorted(set(int(p) for p in presets)):
        if _shard_set_already_present(cc, db_name, n):
            ready.append(n)
    return ready


# ---------------------------------------------------------------------------
# Submit-time partition selection
# ---------------------------------------------------------------------------
def select_partitions_for_submit(
    db_total_bytes: int,
    num_nodes: int,
    machine_type: str,
    *,
    presets: Iterable[int] = PRESET_SHARD_SETS,
) -> int:
    """Pick a shard count for a submit request.

    Two constraints (both v3-validated):

      1. **Node parallelism** — ideally each node owns one shard so no node
         is idle. ``N >= num_nodes``.
      2. **Memory safety** -- each shard must fit comfortably in node RAM,
         otherwise BLAST starts evicting page cache and slows down ~12x
         (v3 sec 4.1). ``shard_size <= 0.5 * node_ram``.

    Returns the smallest preset that satisfies both. Falls back to the
    largest preset if nothing fits.
    """
    if num_nodes < 1:
        raise ValueError(f"num_nodes must be >= 1, got {num_nodes}")
    if db_total_bytes < 0:
        raise ValueError(f"db_total_bytes must be non-negative, got {db_total_bytes}")
    sku = SKU_BY_NAME.get(machine_type)
    if sku is None:
        # Unknown machine type — assume modest 64 GiB. Caller can override
        # by adding the SKU to aks_skus.SKU_CATALOG.
        node_ram_gib = 64
        LOGGER.warning(
            "db_sharding: machine_type %r not in SKU catalog, assuming %d GiB RAM",
            machine_type,
            node_ram_gib,
        )
    else:
        node_ram_gib = sku.memory_gib

    by_nodes = num_nodes
    db_gib = max(1.0, db_total_bytes / float(1024**3))
    safe_node_gib = max(1.0, node_ram_gib * SAFE_SHARD_FRACTION_OF_NODE_RAM)
    by_memory = max(1, int((db_gib + safe_node_gib - 1) // safe_node_gib))
    target = max(by_nodes, by_memory)

    sorted_presets = sorted(set(int(p) for p in presets))
    if not sorted_presets:
        raise ValueError("presets is empty")
    for n in sorted_presets:
        if n >= target:
            return n
    # Target exceeds every preset — return the largest available.
    return sorted_presets[-1]


def partition_prefix_for(
    account_name: str,
    db_name: str,
    num_shards: int,
    *,
    container: str = DEFAULT_CONTAINER,
) -> str:
    """Build the ``db-partition-prefix`` URL for an INI ``[blast]`` section.

    Matches the sibling v3 convention: shard set ``N`` lives at
    ``<container>/{N}shards/<db>_shard_`` and the AKS init script appends
    ``{NN}/`` itself.
    """
    _validate_db_name(db_name)
    _validate_shard_count(num_shards)
    from api.services.storage_endpoint import blob_account_url

    return (
        f"{blob_account_url(account_name)}/{container}/"
        f"{num_shards}shards/{db_name}_shard_"
    )
