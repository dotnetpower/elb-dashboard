#!/opt/elb/venv/bin/python3
"""Generate or validate an elastic-blast.ini from the interactive terminal.

Responsibility: Provide a researcher-friendly `elb-cfg` shell helper that writes
a correct ``elastic-blast.ini`` from environment defaults plus a few flags, so a
person using the browser terminal does not have to hand-author the INI before
running ``elastic-blast submit``.
Edit boundaries: Standalone stdlib-only script bundled into the terminal sidecar
image at ``/usr/local/bin/elb-cfg``. It deliberately mirrors the section/key
layout of ``api/services/blast/config.py`` for the common manual case; it does
NOT reimplement the dashboard's sharding machinery (that stays the authority of
the Cockpit / Celery submit path). Keep it import-light (stdlib only) because the
terminal image has no ``api`` package on PYTHONPATH.
Key entry points: ``main``, ``build_config``, ``check_config``.
Risky contracts: The emitted ``[cloud-provider]`` / ``[cluster]`` / ``[blast]``
keys must stay compatible with upstream elastic-blast and with
``api/services/blast/config.py`` so a manually-authored INI and a dashboard
submit do not diverge. URL expansion must never invent a storage account the
caller did not supply.
Validation: ``python3 -m pytest api/tests/test_elb_cfg_helper.py`` (the helper is
imported as a module there) and a manual ``elb-cfg --print`` smoke run.
"""

from __future__ import annotations

import argparse
import configparser
import io
import os
import sys

# Containers used by the dashboard data plane. A bare path passed to
# --queries / --results / --db is expanded under the matching container.
_CONTAINER_FOR = {
    "queries": "queries",
    "results": "results",
    "db": "blast-db",
}

_DEFAULT_REGION = "koreacentral"
# Keep in sync with api.services.aks_skus.DEFAULT_SKU — the terminal image has
# no `api` package on PYTHONPATH, so this is intentionally a hardcoded mirror.
_DEFAULT_MACHINE_TYPE = "Standard_E32s_v5"
_DEFAULT_PD_SIZE = "3000Gi"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _storage_suffix() -> str:
    # Azure public cloud blob suffix. The terminal sidecar only ever runs in
    # the same cloud as the platform, so a static suffix is sufficient; an
    # explicit --blob-suffix overrides it for sovereign clouds.
    return _env("AZURE_STORAGE_BLOB_SUFFIX", "blob.core.windows.net")


def _derive_storage_container(db_raw: str) -> str:
    """Mirror api.services.blast.config's container derivation exactly.

    Kept logically equivalent to ``config.py`` so a manually authored INI and a
    dashboard submit resolve the same ``azure-storage-account-container``.
    """
    db_raw = (db_raw or "").strip()
    if db_raw and not db_raw.startswith("http"):
        return db_raw.split("/")[0]
    if db_raw.startswith("https://"):
        parts = db_raw.split("/", 4)
        return parts[3] if len(parts) > 3 else "blast-db"
    return "blast-db"


def expand_blob_reference(value: str, *, kind: str, storage_account: str, blob_suffix: str) -> str:
    """Expand a bare path into a full https blob URL when possible.

    A value that already carries a scheme (``https://``, ``azureblob://``) is
    returned unchanged. A bare value like ``my-query.fa`` or
    ``queries/my-query.fa`` is expanded to
    ``https://<account>.<suffix>/<container>/<path>``. If no storage account is
    known the bare value is returned unchanged so the caller can see (and fix)
    the incomplete reference rather than getting a silently wrong URL.
    """
    v = value.strip()
    if not v:
        return ""
    if "://" in v:
        return v
    if not storage_account:
        return v
    container = _CONTAINER_FOR.get(kind, kind)
    path = v.lstrip("/")
    # Allow either "container/path" (use as-is) or "path" (prepend container).
    first = path.split("/", 1)[0]
    if first in _CONTAINER_FOR.values():
        rel = path
    else:
        rel = f"{container}/{path}"
    return f"https://{storage_account}.{blob_suffix}/{rel}"


def build_config(args: argparse.Namespace) -> configparser.ConfigParser:
    """Build a ConfigParser matching the elastic-blast.ini contract."""
    storage_account = args.storage_account
    blob_suffix = args.blob_suffix

    cfg = configparser.ConfigParser()

    # [cloud-provider]
    cfg.add_section("cloud-provider")
    cfg.set("cloud-provider", "azure-region", args.region)
    cfg.set("cloud-provider", "azure-resource-group", args.resource_group)
    if args.acr_resource_group:
        cfg.set("cloud-provider", "azure-acr-resource-group", args.acr_resource_group)
    if args.acr_name:
        cfg.set("cloud-provider", "azure-acr-name", args.acr_name)
    if storage_account:
        cfg.set("cloud-provider", "azure-storage-account", storage_account)
    # Always derive the container, mirroring config.py (which sets it whether or
    # not a storage account is known) so a manual INI and a dashboard submit
    # resolve the same azure-storage-account-container.
    cfg.set("cloud-provider", "azure-storage-account-container", _derive_storage_container(args.db))

    # [cluster]
    cfg.add_section("cluster")
    cfg.set("cluster", "name", args.name)
    cfg.set("cluster", "machine-type", args.machine_type)
    cfg.set("cluster", "num-nodes", str(args.num_nodes))
    cfg.set("cluster", "pd-size", args.pd_size)
    # The dashboard runs only the AKS node-local SSD path; keep manual configs
    # aligned so an INI authored here behaves like a dashboard submit.
    cfg.set("cluster", "exp-use-local-ssd", "true")

    # [blast]
    cfg.add_section("blast")
    cfg.set("blast", "program", args.program)
    cfg.set("blast", "db", args.db)
    cfg.set(
        "blast",
        "queries",
        expand_blob_reference(
            args.queries, kind="queries", storage_account=storage_account, blob_suffix=blob_suffix
        ),
    )
    cfg.set(
        "blast",
        "results",
        expand_blob_reference(
            args.results, kind="results", storage_account=storage_account, blob_suffix=blob_suffix
        ),
    )
    if args.options:
        cfg.set("blast", "options", args.options)
    return cfg


def config_to_text(cfg: configparser.ConfigParser) -> str:
    buf = io.StringIO()
    cfg.write(buf)
    return buf.getvalue()


# Keys the dashboard submit path / upstream elastic-blast require to be present
# and non-empty for an Azure run. Used by both generation warnings and --check.
_REQUIRED = {
    "cloud-provider": ["azure-region", "azure-resource-group"],
    "cluster": ["name", "machine-type", "num-nodes"],
    "blast": ["program", "db", "queries", "results"],
}


def missing_required(cfg: configparser.ConfigParser) -> list[str]:
    gaps: list[str] = []
    for section, keys in _REQUIRED.items():
        if not cfg.has_section(section):
            gaps.append(f"[{section}] (whole section missing)")
            continue
        for key in keys:
            if not (cfg.get(section, key, fallback="") or "").strip():
                gaps.append(f"[{section}] {key}")
    return gaps


def check_config(path: str) -> int:
    cfg = configparser.ConfigParser()
    try:
        with open(path, encoding="utf-8") as fh:
            cfg.read_file(fh)
    except OSError as exc:
        print(f"elb-cfg: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    except configparser.Error as exc:
        print(f"elb-cfg: {path} is not a valid INI: {exc}", file=sys.stderr)
        return 2
    gaps = missing_required(cfg)
    if gaps:
        print(f"elb-cfg: {path} is missing required keys:", file=sys.stderr)
        for gap in gaps:
            print(f"  - {gap}", file=sys.stderr)
        return 1
    total = sum(len(v) for v in _REQUIRED.values())
    print(f"elb-cfg: {path} looks complete ({total} required keys present).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="elb-cfg",
        description=(
            "Generate or validate an elastic-blast.ini for ElasticBLAST on Azure. "
            "Platform coordinates (region, resource group, storage account, ACR) "
            "default from the terminal environment; override with flags."
        ),
        epilog=(
            "Examples:\n"
            "  elb-cfg --program blastn --db blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA \\\n"
            "          --queries my-query.fa --results results/run-001 -o elastic-blast.ini\n"
            "  elb-cfg --check elastic-blast.ini\n"
            "Then: elastic-blast submit --cfg elastic-blast.ini\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--check", metavar="FILE", help="Validate an existing INI and exit.")
    p.add_argument("--program", default="blastn", help="BLAST program (blastn, blastp, ...).")
    p.add_argument("--db", default="", help="Database path or URL (e.g. blast-db/<name>/<name>).")
    p.add_argument(
        "--queries",
        default="",
        help="Query file/URL. A bare name expands under the queries container.",
    )
    p.add_argument(
        "--results",
        default="",
        help="Results path/URL. A bare name expands under the results container.",
    )
    p.add_argument(
        "--options", default="", help="Extra BLAST options string (e.g. '-evalue 1e-5')."
    )
    p.add_argument("--name", default="", help="AKS cluster name (default: elastic-blast-<user>).")
    p.add_argument(
        "--machine-type", dest="machine_type", default=_DEFAULT_MACHINE_TYPE, help="VM SKU."
    )
    p.add_argument("--num-nodes", dest="num_nodes", type=int, default=1, help="Node count (>=1).")
    p.add_argument(
        "--pd-size", dest="pd_size", default=_DEFAULT_PD_SIZE, help="Persistent disk size."
    )
    p.add_argument("--region", default="", help="Azure region (default: $AZURE_REGION).")
    p.add_argument(
        "--rg",
        dest="resource_group",
        default="",
        help="Resource group (default: $AZURE_RESOURCE_GROUP).",
    )
    p.add_argument(
        "--storage-account",
        dest="storage_account",
        default="",
        help="Storage account (default: $STORAGE_ACCOUNT_NAME).",
    )
    p.add_argument(
        "--acr-name", dest="acr_name", default="", help="ACR name (default: $PLATFORM_ACR_NAME)."
    )
    p.add_argument(
        "--acr-rg",
        dest="acr_resource_group",
        default="",
        help="ACR resource group (default: resource group).",
    )
    p.add_argument(
        "--blob-suffix",
        dest="blob_suffix",
        default="",
        help="Storage blob suffix for URL expansion.",
    )
    p.add_argument("-o", "--output", default="", help="Write INI to this file (default: stdout).")
    p.add_argument("--force", action="store_true", help="Overwrite --output if it already exists.")
    return p


def _apply_env_defaults(args: argparse.Namespace) -> None:
    args.region = args.region or _env("AZURE_REGION", _DEFAULT_REGION)
    args.resource_group = args.resource_group or _env("AZURE_RESOURCE_GROUP")
    args.storage_account = args.storage_account or _env("STORAGE_ACCOUNT_NAME")
    args.acr_name = args.acr_name or _env("PLATFORM_ACR_NAME")
    args.acr_resource_group = args.acr_resource_group or args.resource_group
    args.blob_suffix = args.blob_suffix or _storage_suffix()
    if not args.name:
        user = _env("USER", "azureuser")
        args.name = f"elastic-blast-{user}"[:40]
    if args.num_nodes < 1:
        args.num_nodes = 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        return check_config(args.check)

    _apply_env_defaults(args)
    cfg = build_config(args)
    text = config_to_text(cfg)

    if args.output:
        if os.path.exists(args.output) and not args.force:
            print(
                f"elb-cfg: {args.output} already exists; pass --force to overwrite.",
                file=sys.stderr,
            )
            return 1
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            print(f"elb-cfg: cannot write {args.output}: {exc}", file=sys.stderr)
            return 2
        print(f"elb-cfg: wrote {args.output}")
    else:
        sys.stdout.write(text)

    gaps = missing_required(cfg)
    if gaps:
        print("elb-cfg: WARNING — these required keys are still empty:", file=sys.stderr)
        for gap in gaps:
            print(f"  - {gap}", file=sys.stderr)
        print(
            "elb-cfg: fill them with flags (e.g. --db / --queries / --results) "
            "before `elastic-blast submit`.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
