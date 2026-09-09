"""Estimate BLAST runtime and compute cost from comparable completed jobs.

Responsibility: Extract trustworthy runtime/work-size observations from JobState rows and build
    a robust, evidence-gated estimate for one proposed job.
Edit boundaries: Pure calculation over caller-supplied rows plus the existing cost estimator;
    routes own repository reads and HTTP/auth shaping.
Key entry points: ``RuntimeEstimateRequest``, ``estimate_runtime_cost``.
Risky contracts: Never fabricate a duration from static multipliers. Fewer than three comparable
    samples returns unavailable; outliers and partial metadata are excluded explicitly.
Validation: ``uv run pytest -q api/tests/test_blast_runtime_estimate.py``.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from api.services.blast.submit_payload import canonical_submit_snapshot
from api.services.cost.estimate import estimate_cluster_cost
from api.services.web_blast_searchsp import database_name_from_path

_MIN_SAMPLES = 3
_MAX_SAMPLES = 500


class RuntimeEstimateRequest(BaseModel):
    """Scientific/runtime inputs for an evidence-based estimate."""

    subscription_id: str = Field(..., min_length=1, max_length=64)
    resource_group: str = Field(..., min_length=1, max_length=90)
    cluster_name: str = Field(..., min_length=1, max_length=255)
    program: str = Field(..., min_length=1, max_length=32)
    database: str = Field(..., min_length=1, max_length=1024)
    query_letters: int = Field(..., ge=1, le=100_000_000)
    database_letters: int = Field(..., ge=1)
    node_count: int = Field(..., ge=1, le=1000)
    node_sku: str = Field(..., min_length=1, max_length=128)
    region: str = Field(default="", max_length=64)


class RuntimeCostEstimate(BaseModel):
    """Available estimate or an explicit evidence-gap response."""

    schema_version: int = 1
    available: bool
    reason: str | None = None
    sample_count: int
    required_samples: int = _MIN_SAMPLES
    estimate_seconds: int | None = None
    low_seconds: int | None = None
    high_seconds: int | None = None
    confidence: str | None = None
    estimated_cost_usd: float | None = None
    pricing: dict[str, Any] | None = None
    basis: dict[str, Any]


@dataclass(frozen=True)
class RuntimeObservation:
    """One normalized completed-job throughput observation."""

    rate_per_node: float
    duration_seconds: float


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_number(*values: object) -> float | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                continue
        else:
            continue
        if math.isfinite(number) and number > 0:
            return number
    return None


def _duration_seconds(payload: dict[str, Any]) -> float | None:
    external = _mapping(payload.get("external"))
    progress = _mapping(payload.get("_progress"))
    steps = _mapping(progress.get("steps"))
    running = _mapping(steps.get("running"))
    k8s = _mapping(running.get("k8s"))
    duration_ms = _positive_number(k8s.get("blast_container_duration_ms"))
    if duration_ms is not None:
        return duration_ms / 1000.0
    return _positive_number(
        external.get("run_seconds"),
        payload.get("run_seconds"),
        running.get("duration_seconds"),
    )


def _observation(state: Any, request: RuntimeEstimateRequest) -> RuntimeObservation | None:
    if str(getattr(state, "status", "") or "").casefold() not in {
        "completed",
        "succeeded",
        "success",
    }:
        return None
    payload = _mapping(getattr(state, "payload", None))
    external = _mapping(payload.get("external"))
    snapshot = _mapping(payload.get("canonical_request"))
    if not snapshot:
        snapshot = canonical_submit_snapshot(payload)
    query = _mapping(snapshot.get("query"))
    options = _mapping(snapshot.get("options"))
    config = _mapping(payload.get("config_snapshot"))
    if not config:
        config = _mapping(external.get("config_snapshot"))

    program = str(
        getattr(state, "program", "")
        or snapshot.get("program")
        or payload.get("program")
        or external.get("program")
        or ""
    ).casefold()
    database = database_name_from_path(
        str(
            getattr(state, "db", "")
            or snapshot.get("database")
            or payload.get("db")
            or external.get("db_name")
            or external.get("db")
            or ""
        )
    ).casefold()
    node_sku = str(
        payload.get("machine_type")
        or options.get("machine_type")
        or config.get("machine_type")
        or ""
    ).casefold()
    if program != request.program.casefold():
        return None
    if database != database_name_from_path(request.database).casefold():
        return None
    if not node_sku or node_sku != request.node_sku.casefold():
        return None

    query_letters = _positive_number(
        query.get("total_letters"),
        _mapping(payload.get("query_metadata")).get("total_letters"),
        payload.get("query_length"),
        external.get("query_length"),
    )
    database_letters = _positive_number(
        payload.get("db_total_letters"),
        config.get("db_total_letters"),
        _mapping(payload.get("database_metadata")).get("number_of_letters"),
    )
    node_count = _positive_number(
        payload.get("num_nodes"),
        options.get("num_nodes"),
        config.get("num_nodes"),
    )
    duration = _duration_seconds(payload)
    if (
        query_letters is None
        or database_letters is None
        or node_count is None
        or duration is None
    ):
        return None
    rate = (query_letters * database_letters) / (duration * node_count)
    if not math.isfinite(rate) or rate <= 0:
        return None
    return RuntimeObservation(rate_per_node=rate, duration_seconds=duration)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def estimate_runtime_cost(
    request: RuntimeEstimateRequest,
    states: list[Any],
) -> RuntimeCostEstimate:
    """Build a robust estimate or return an explicit insufficient-data result."""
    observations = [
        observation
        for state in states[:_MAX_SAMPLES]
        if (observation := _observation(state, request)) is not None
    ]
    basis = {
        "program": request.program,
        "database": database_name_from_path(request.database),
        "node_sku": request.node_sku,
        "node_count": request.node_count,
        "query_letters": request.query_letters,
        "database_letters": request.database_letters,
        "method": "median_normalized_throughput",
    }
    if len(observations) < _MIN_SAMPLES:
        return RuntimeCostEstimate(
            available=False,
            reason="insufficient_samples",
            sample_count=len(observations),
            basis=basis,
        )

    rates = [observation.rate_per_node for observation in observations]
    median_rate = statistics.median(rates)
    # Remove only extreme instrumentation/unit errors; natural runtime spread
    # inside a 4x band remains evidence and is reflected in the interval.
    filtered = [rate for rate in rates if median_rate / 4 <= rate <= median_rate * 4]
    if len(filtered) < _MIN_SAMPLES:
        return RuntimeCostEstimate(
            available=False,
            reason="insufficient_consistent_samples",
            sample_count=len(filtered),
            basis=basis,
        )
    work = float(request.query_letters) * float(request.database_letters)
    predicted = [work / (rate * request.node_count) for rate in filtered]
    estimate_seconds = max(1, round(statistics.median(predicted)))
    low_seconds = max(1, round(_percentile(predicted, 0.25)))
    high_seconds = max(low_seconds, round(_percentile(predicted, 0.75)))
    relative_spread = (high_seconds - low_seconds) / estimate_seconds
    confidence = (
        "high"
        if len(filtered) >= 10 and relative_spread <= 0.35
        else "medium"
        if len(filtered) >= 5 and relative_spread <= 0.75
        else "low"
    )
    cost = estimate_cluster_cost(
        node_sku=request.node_sku,
        node_count=request.node_count,
        uptime_seconds=estimate_seconds,
        running=True,
        region=request.region,
    )
    return RuntimeCostEstimate(
        available=True,
        sample_count=len(filtered),
        estimate_seconds=estimate_seconds,
        low_seconds=low_seconds,
        high_seconds=high_seconds,
        confidence=confidence,
        estimated_cost_usd=(
            round(cost.accrued_usd, 4) if cost.priced and cost.accrued_usd is not None else None
        ),
        pricing={
            "priced": cost.priced,
            "source": cost.priced_source,
            "priced_as_of": cost.priced_as_of,
            "hourly_usd": round(cost.hourly_usd, 4),
            "is_estimate": True,
        },
        basis=basis,
    )
