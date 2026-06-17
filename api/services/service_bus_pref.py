"""Service Bus integration configuration (single deployment-wide row).

Responsibility: Persist and read the optional Service Bus BLAST integration
    configuration — enable switch, auth mode (Entra/SAS), namespace + queue +
    optional topic name, BLAST routing context, and the dead-letter cleanup policy.
    There is exactly ONE config row per deployment (PartitionKey
    ``servicebus_config`` / RowKey ``current``); this is not a per-cluster
    preference like ``performance_pref``.
Edit boundaries: Reusable domain/persistence logic only. HTTP shaping lives in
    ``api.routes.settings.service_bus``; the data-plane client lives in
    ``api.services.service_bus``. No Azure SDK management/data-plane calls here.
Key entry points: ``ServiceBusConfig``, ``AUTH_MODES``, ``get_service_bus_config``,
    ``save_service_bus_config``, ``service_bus_enabled``, ``normalise_config``.
Risky contracts: ``enabled`` defaults to ``False`` and a missing row reads back
    as a disabled default — the integration must stay off until an operator
    opts in (charter §12a Rule 4). The SAS connection string itself is NEVER
    stored in this row; only the Key Vault secret name is. Table backend is
    gated by ``AZURE_TABLE_ENDPOINT`` + ``CONTAINER_APP_NAME`` (mirrors
    ``performance_pref``); local dev falls back to a JSON file.
Validation: ``uv run pytest -q api/tests/test_service_bus_pref.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

AUTH_MODE_ENTRA = "entra"
AUTH_MODE_SAS = "sas"
AUTH_MODES: tuple[str, ...] = (AUTH_MODE_ENTRA, AUTH_MODE_SAS)
DEFAULT_AUTH_MODE = AUTH_MODE_ENTRA

DEFAULT_REQUEST_QUEUE = "elastic-blast-requests"
DEFAULT_COMPLETION_TOPIC = "elastic-blast-completions"
DEFAULT_DLQ_MAX_AGE_DAYS = 7
DEFAULT_DLQ_MAX_COUNT = 5000
DEFAULT_DLQ_CLEANUP_BATCH = 500

# Deployment-level entity-name overrides. When SET (non-empty) they win over the
# saved Table/file config so an operator can pin the request queue / completion
# topic via the Container App env without editing the Settings row (charter
# §12a Rule 4: unset = existing behaviour preserved, i.e. the saved config value
# — or its default — is used). An env value that fails the entity-name regex is
# ignored (logged) so a typo can never silently point the integration at a bad
# entity. ``SERVICEBUS_RESPONSE_TOPIC`` is the completion topic the dashboard
# publishes transition events to and external subscribers consume.
_REQUEST_QUEUE_ENV = "SERVICEBUS_REQUEST_QUEUE"
_RESPONSE_TOPIC_ENV = "SERVICEBUS_RESPONSE_TOPIC"

# Bounds — keep the cleanup task bounded (charter self-critique: no runaway loop).
_DLQ_MAX_AGE_DAYS_CEIL = 365
_DLQ_MAX_COUNT_CEIL = 1_000_000
_DLQ_CLEANUP_BATCH_CEIL = 2000

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "servicebuspref"
_TYPE = "servicebus_config"
_PARTITION_KEY = "servicebus_config"
_ROW_KEY = "current"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"

# Service Bus entity naming: queues/topics allow letters, digits, and
# .-_/~ with length 1..260. FQDN is a hostname ending in a known SB suffix.
_RE_FQDN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]{1,250}\.servicebus\.windows\.net$")
_RE_ENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/~]{0,259}$")
_RE_SECRET_NAME = re.compile(r"^[A-Za-z0-9-]{1,127}$")

_ENSURED_TABLES: set[str] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_SB_TABLE_POOLED: TableClient | None = None
_SB_TABLE_POOL_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_auth_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in AUTH_MODES else DEFAULT_AUTH_MODE


def _clean_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clean_int(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, out))


@dataclass
class ServiceBusConfig:
    """Deployment-wide Service Bus integration config (one row)."""

    enabled: bool = False
    auth_mode: str = DEFAULT_AUTH_MODE
    namespace_fqdn: str = ""
    request_queue: str = DEFAULT_REQUEST_QUEUE
    completion_topic: str = DEFAULT_COMPLETION_TOPIC
    # SAS mode only: the Key Vault secret NAME holding the connection string.
    # The connection string itself is never persisted in this row.
    sas_secret_name: str = ""
    # BLAST routing context — which cluster/storage a drained request runs on.
    subscription_id: str = ""
    resource_group: str = ""
    cluster_name: str = ""
    storage_account: str = ""
    # Dead-letter cleanup policy.
    dlq_cleanup_enabled: bool = False
    dlq_max_age_days: int = DEFAULT_DLQ_MAX_AGE_DAYS
    dlq_max_count: int = DEFAULT_DLQ_MAX_COUNT
    dlq_cleanup_batch: int = DEFAULT_DLQ_CLEANUP_BATCH
    # Provenance.
    updated_at: str = ""
    owner_oid: str = ""
    tenant_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auth_mode": self.auth_mode,
            "namespace_fqdn": self.namespace_fqdn,
            "request_queue": self.request_queue,
            "completion_topic": self.completion_topic,
            "sas_secret_name": self.sas_secret_name,
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "cluster_name": self.cluster_name,
            "storage_account": self.storage_account,
            "dlq_cleanup_enabled": self.dlq_cleanup_enabled,
            "dlq_max_age_days": self.dlq_max_age_days,
            "dlq_max_count": self.dlq_max_count,
            "dlq_cleanup_batch": self.dlq_cleanup_batch,
            "updated_at": self.updated_at,
            "owner_oid": self.owner_oid,
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ServiceBusConfig:
        completion_topic_value = value.get("completion_topic", DEFAULT_COMPLETION_TOPIC)
        return cls(
            enabled=_clean_bool(value.get("enabled")),
            auth_mode=_clean_auth_mode(value.get("auth_mode")),
            namespace_fqdn=str(value.get("namespace_fqdn") or ""),
            request_queue=str(value.get("request_queue") or DEFAULT_REQUEST_QUEUE),
            completion_topic="" if completion_topic_value is None else str(completion_topic_value),
            sas_secret_name=str(value.get("sas_secret_name") or ""),
            subscription_id=str(value.get("subscription_id") or ""),
            resource_group=str(value.get("resource_group") or ""),
            cluster_name=str(value.get("cluster_name") or ""),
            storage_account=str(value.get("storage_account") or ""),
            dlq_cleanup_enabled=_clean_bool(value.get("dlq_cleanup_enabled")),
            dlq_max_age_days=_clean_int(
                value.get("dlq_max_age_days"),
                DEFAULT_DLQ_MAX_AGE_DAYS,
                low=1,
                high=_DLQ_MAX_AGE_DAYS_CEIL,
            ),
            dlq_max_count=_clean_int(
                value.get("dlq_max_count"),
                DEFAULT_DLQ_MAX_COUNT,
                low=1,
                high=_DLQ_MAX_COUNT_CEIL,
            ),
            dlq_cleanup_batch=_clean_int(
                value.get("dlq_cleanup_batch"),
                DEFAULT_DLQ_CLEANUP_BATCH,
                low=1,
                high=_DLQ_CLEANUP_BATCH_CEIL,
            ),
            updated_at=str(value.get("updated_at") or ""),
            owner_oid=str(value.get("owner_oid") or ""),
            tenant_id=str(value.get("tenant_id") or ""),
        )

    def public_dict(self) -> dict[str, Any]:
        """Config safe to return to the browser (no secret material).

        ``sas_secret_name`` is a Key Vault secret *name*, not the secret value,
        so it is safe to surface (lets the UI show which secret is wired).
        """
        return self.to_dict()


def normalise_config(
    value: dict[str, Any], *, owner_oid: str = "", tenant_id: str = ""
) -> ServiceBusConfig:
    """Validate an incoming config dict and stamp provenance.

    Raises ``ValueError`` with a stable message on contract violations so the
    route can return HTTP 400 with a useful detail.
    """
    cfg = ServiceBusConfig.from_dict(value)

    if cfg.enabled:
        if not _RE_FQDN.match(cfg.namespace_fqdn):
            raise ValueError(
                "namespace_fqdn must be a *.servicebus.windows.net hostname when enabled"
            )
        if not _RE_ENTITY.match(cfg.request_queue):
            raise ValueError("request_queue is not a valid Service Bus entity name")
        if cfg.completion_topic and not _RE_ENTITY.match(cfg.completion_topic):
            raise ValueError("completion_topic is not a valid Service Bus entity name")
        if cfg.auth_mode == AUTH_MODE_SAS and not _RE_SECRET_NAME.match(cfg.sas_secret_name):
            raise ValueError("sas_secret_name is required (Key Vault secret name) in SAS mode")
    # Routing context is validated lazily at drain time (a config can be saved
    # disabled or before the cluster is chosen); only sanity-check format here.
    if cfg.namespace_fqdn and not _RE_FQDN.match(cfg.namespace_fqdn):
        raise ValueError("namespace_fqdn must be a *.servicebus.windows.net hostname")

    cfg.updated_at = _now_iso()
    cfg.owner_oid = owner_oid or cfg.owner_oid
    cfg.tenant_id = tenant_id or cfg.tenant_id
    return cfg


def service_bus_env_gate_on() -> bool:
    """True when the deployment master switch ``SERVICEBUS_ENABLED`` is on.

    This reflects ONLY the env gate (``SERVICEBUS_ENABLED`` in
    ``control-plane-env.json`` / the Container App env), independent of the
    saved config row. The Settings UI uses it to explain precisely why an
    operator-enabled config is still not live: the deployment never opted in,
    so the integration stays dormant regardless of the runtime toggle.
    """
    return str(os.environ.get("SERVICEBUS_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def service_bus_enabled() -> bool:
    """True only when BOTH the env gate AND the saved config say enabled.

    The env flag ``SERVICEBUS_ENABLED`` is the deployment-level master switch
    (default off, set per-sidecar in ``control-plane-env.json``); the saved
    config row is the operator's runtime toggle. Both must agree, so a stale
    config row can never re-activate the subsystem on a deployment that did not
    opt in, and vice-versa.
    """
    if not service_bus_env_gate_on():
        return False
    cfg = get_service_bus_config()
    return cfg.enabled and bool(cfg.namespace_fqdn)


# --------------------------------------------------------------------------- #
# Persistence (Table in Container Apps, JSON file locally) — mirrors
# performance_pref so the same RBAC/backend gating applies.
# --------------------------------------------------------------------------- #


def _use_table_backend() -> bool:
    return bool(os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME"))


def get_service_bus_config() -> ServiceBusConfig:
    """Return the saved config, or a disabled default when no row exists."""
    if _use_table_backend():
        found = _get_table()
    else:
        found = _get_file()
    cfg = found if found is not None else ServiceBusConfig()
    return _apply_entity_env_overrides(cfg)


def _apply_entity_env_overrides(cfg: ServiceBusConfig) -> ServiceBusConfig:
    """Overlay ``SERVICEBUS_REQUEST_QUEUE`` / ``SERVICEBUS_RESPONSE_TOPIC``.

    A non-empty, well-formed env value wins over the saved entity name so a
    deployment can pin the request queue / completion topic without editing the
    Settings row. Unset env keys leave the config untouched (existing behaviour
    preserved). A malformed env value is ignored (logged) — never silently
    points the integration at an invalid entity. Mutates and returns ``cfg`` in
    place; ``cfg`` is a fresh object per call so this never leaks across rows.
    """
    queue_override = os.environ.get(_REQUEST_QUEUE_ENV, "").strip()
    if queue_override:
        if _RE_ENTITY.match(queue_override):
            cfg.request_queue = queue_override
        else:
            LOGGER.warning(
                "%s=%r is not a valid Service Bus entity name; ignoring override",
                _REQUEST_QUEUE_ENV,
                queue_override,
            )
    topic_override = os.environ.get(_RESPONSE_TOPIC_ENV, "").strip()
    if topic_override:
        if _RE_ENTITY.match(topic_override):
            cfg.completion_topic = topic_override
        else:
            LOGGER.warning(
                "%s=%r is not a valid Service Bus entity name; ignoring override",
                _RESPONSE_TOPIC_ENV,
                topic_override,
            )
    return cfg


def save_service_bus_config(cfg: ServiceBusConfig) -> ServiceBusConfig:
    if _use_table_backend():
        _save_table(cfg)
    else:
        _save_file(cfg)
    return cfg


def _entity_from_config(cfg: ServiceBusConfig) -> dict[str, Any]:
    return {
        "PartitionKey": _PARTITION_KEY,
        "RowKey": _ROW_KEY,
        "type": _TYPE,
        "enabled": cfg.enabled,
        "updated_at": cfg.updated_at or _now_iso(),
        "owner_oid": cfg.owner_oid,
        "tenant_id": cfg.tenant_id,
        "payload_json": json.dumps(cfg.to_dict(), default=str),
    }


def _config_from_entity(entity: dict[str, Any]) -> ServiceBusConfig | None:
    try:
        payload = json.loads(str(entity.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return None
    return ServiceBusConfig.from_dict(payload)


def _table_client() -> TableClient:
    global _SB_TABLE_POOLED
    pool = _SB_TABLE_POOLED
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _SB_TABLE_POOL_LOCK:
        if _SB_TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _SB_TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _SB_TABLE_POOLED  # type: ignore[return-value]


def _reset_service_bus_table_pool() -> None:
    """Test hook + safety valve for credential reset."""
    global _SB_TABLE_POOLED
    with _SB_TABLE_POOL_LOCK:
        pool = _SB_TABLE_POOLED
        _SB_TABLE_POOLED = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    if endpoint in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if endpoint in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(endpoint)


def _save_table(cfg: ServiceBusConfig) -> None:
    _ensure_table()
    entity = _entity_from_config(cfg)
    with _table_client() as table:
        table.upsert_entity(entity, mode=UpdateMode.REPLACE)


def _get_table() -> ServiceBusConfig | None:
    _ensure_table()
    with _table_client() as table:
        try:
            entity = table.get_entity(partition_key=_PARTITION_KEY, row_key=_ROW_KEY)
        except ResourceNotFoundError:
            return None
        entity_dict = dict(entity)
    return _config_from_entity(entity_dict)


# --------------------------------------------------------------------------- #
# Local JSON file backend (workstation dev without Table RBAC).
# --------------------------------------------------------------------------- #

_FILE_LOCK = threading.Lock()


def _state_file() -> Path:
    default_root = Path(__file__).resolve().parents[2] / ".logs" / "local" / "state"
    root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
    return root / "service_bus_config.json"


def _get_file() -> ServiceBusConfig | None:
    path = _state_file()
    if not path.exists():
        return None
    try:
        data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    return ServiceBusConfig.from_dict(data)


def _save_file(cfg: ServiceBusConfig) -> None:
    path = _state_file()
    with _FILE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(cfg.to_dict(), default=str, indent=2), encoding="utf-8")
        tmp.replace(path)
