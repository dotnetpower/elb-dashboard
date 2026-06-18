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
    ``save_service_bus_config``, ``service_bus_enabled``, ``service_bus_enabled_for``,
    ``service_bus_env_override``, ``service_bus_kill_switch_on``, ``normalise_config``.
Risky contracts: ``enabled`` defaults to ``False`` and a missing row reads back
    as a disabled default — the integration stays off until an operator opts in
    (charter §12a Rule 4 default-OFF preserved). The deploy-time env
    ``SERVICEBUS_ENABLED`` is a three-state *override* (truthy = pin capability
    on but still require config; falsy = kill switch forcing OFF; unset = defer
    to the config row), NOT a hard AND-gate — see ``service_bus_enabled`` /
    ``service_bus_env_override``. So the Settings toggle is a runtime feature
    flag that survives redeploys, while a deployment retains an explicit kill
    switch. The SAS connection string itself is NEVER stored in this row; only
    the Key Vault secret name is. Table backend is gated by
    ``AZURE_TABLE_ENDPOINT`` + ``CONTAINER_APP_NAME`` (mirrors
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

# Completion-entity kind. A topic fans every transition event out to many
# independent subscriptions (the dashboard observer gets its own copy without
# competing with an external subscriber); a queue is point-to-point, so a single
# competing consumer drains it. Default "topic" preserves the historical
# fan-out behaviour (charter §12a Rule 4: unset = existing behaviour).
COMPLETION_KIND_TOPIC = "topic"
COMPLETION_KIND_QUEUE = "queue"
COMPLETION_KINDS: tuple[str, ...] = (COMPLETION_KIND_TOPIC, COMPLETION_KIND_QUEUE)
DEFAULT_COMPLETION_KIND = COMPLETION_KIND_TOPIC

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
# Deployment-level override for the completion-entity kind (topic|queue). Like
# the entity-name overrides above, a well-formed env value wins over the saved
# config so an operator can run queue/queue without editing the Settings row;
# an unrecognised value is ignored (logged) and the saved/default kind stands.
_COMPLETION_KIND_ENV = "SERVICEBUS_COMPLETION_KIND"

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


def _clean_completion_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in COMPLETION_KINDS else DEFAULT_COMPLETION_KIND


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
    # Completion entity kind: "topic" (fan-out) or "queue" (point-to-point).
    completion_kind: str = DEFAULT_COMPLETION_KIND
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
            "completion_kind": self.completion_kind,
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
            completion_kind=_clean_completion_kind(value.get("completion_kind")),
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


_ENV_TRUTHY = {"1", "true", "yes", "on"}
_ENV_FALSY = {"0", "false", "no", "off"}


def service_bus_env_override() -> bool | None:
    """Three-state deploy-time override for the Service Bus feature flag.

    The env var ``SERVICEBUS_ENABLED`` (set per-sidecar in
    ``control-plane-env.json`` / the Container App revision) is no longer a hard
    master switch but a deploy-time *override* of the runtime config:

    * ``True``  — explicitly truthy (``true``/``1``/``yes``/``on``): the
      deployment pins the capability ON. Activation still requires the saved
      config (``enabled`` + namespace); the env never bypasses the config.
    * ``False`` — explicitly falsy (``false``/``0``/``no``/``off``): a
      deployment-level **kill switch**. The integration stays OFF regardless of
      the saved config — the operator override of last resort.
    * ``None``  — unset / empty / unrecognised: **defer to the saved config
      row**, so the Settings toggle behaves as a runtime feature flag that
      survives redeploys (the config lives in the Table, not on the revision).

    The repo default in ``control-plane-env.json`` is empty (``None`` → defer),
    so a fresh deployment stays OFF until an authenticated operator opts in via
    Settings (the config defaults to ``enabled=False``); default-OFF is
    preserved (charter §12a Rule 4) while the deployment keeps an explicit
    kill switch.
    """
    raw = str(os.environ.get("SERVICEBUS_ENABLED", "")).strip().lower()
    if raw in _ENV_TRUTHY:
        return True
    if raw in _ENV_FALSY:
        return False
    return None


def service_bus_env_gate_on() -> bool:
    """True when the deployment explicitly pins ``SERVICEBUS_ENABLED`` truthy.

    NOTE: since the gate became a three-state override
    (``service_bus_env_override``), an explicit truthy env is no longer
    *required* to activate — an unset env defers to the saved config. This
    helper reports only the "explicitly pinned ON" state and is surfaced in the
    Settings status payload for diagnostics; do not use it as the activation
    gate (use ``service_bus_enabled``).
    """
    return service_bus_env_override() is True


def service_bus_kill_switch_on() -> bool:
    """True when ``SERVICEBUS_ENABLED`` is explicitly falsy.

    The deployment kill switch: forces the integration OFF regardless of the
    saved config row. Surfaced in the Settings status payload so the SPA can
    explain the rare "enabled in settings but a deployment override is forcing
    it off" state, distinct from "no namespace configured yet".
    """
    return service_bus_env_override() is False


def service_bus_enabled_for(cfg: ServiceBusConfig) -> bool:
    """Gate result for an ALREADY-READ config snapshot.

    Identical rule to ``service_bus_enabled`` but the caller supplies the config
    (avoids a redundant Table read, and keeps a single request reasoning over one
    consistent snapshot). ``service_bus_enabled`` is the convenience wrapper that
    reads the current config first.
    """
    if service_bus_kill_switch_on():
        return False
    return cfg.enabled and bool(cfg.namespace_fqdn)


def service_bus_enabled() -> bool:
    """True when the integration is live: not kill-switched AND the saved config
    opts in (``enabled`` + namespace).

    ``SERVICEBUS_ENABLED`` is a three-state deploy-time override (see
    ``service_bus_env_override``): an explicit falsy value forces OFF (kill
    switch); explicit truthy or unset both defer to the saved config row, which
    is the runtime feature flag. Because the config lives in the Table (not on
    the Container App revision), toggling it in Settings takes effect at the
    next gate check — consistently across all sidecars, which read the same
    row — and survives redeploys, instead of being reset to a revision-baked
    env default.
    """
    return service_bus_enabled_for(get_service_bus_config())


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
    kind_override = os.environ.get(_COMPLETION_KIND_ENV, "").strip().lower()
    if kind_override:
        if kind_override in COMPLETION_KINDS:
            cfg.completion_kind = kind_override
        else:
            LOGGER.warning(
                "%s=%r is not one of %s; ignoring override",
                _COMPLETION_KIND_ENV,
                kind_override,
                COMPLETION_KINDS,
            )
    return cfg


def completion_is_queue(cfg: ServiceBusConfig) -> bool:
    """True when the completion entity is a queue (point-to-point), not a topic.

    Tolerant of objects (e.g. test ``SimpleNamespace``) that predate the
    ``completion_kind`` field — a missing attribute reads as the default topic
    kind so existing callers keep the historical fan-out behaviour.
    """
    return getattr(cfg, "completion_kind", DEFAULT_COMPLETION_KIND) == COMPLETION_KIND_QUEUE


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
