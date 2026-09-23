"""Phase 11.6 usage KPI views over the versioned canonical event contract.

This module is deliberately a pure, bounded aggregation layer.  It accepts
already routed canonical events (or dictionaries with the same wire shape),
deduplicates them by the Phase 11 event identity, and returns a versioned
dashboard contract.  It never reads BigQuery, writes a ledger synchronously
from a request, or uses summary attachments as KPI input.

The production pipeline can use :class:`ActivationLedger` as the small
first-success table and feed its results to :func:`build_kpi_view`.  The
functions are also usable with synthetic fixtures so the counting grain and
history degradation rules can be tested without cloud credentials.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Iterable, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from semantic_catalog import semantic_catalog
from usage_contract import UsageEvent
from traffic_summary_report import TRAFFIC_METRICS


KPI_SCHEMA_VERSION = "1.0"
DEFAULT_TIME_ZONE = "Asia/Taipei"
DEFAULT_MEASUREMENT_VERSION = "v1"
SESSION_GAP = timedelta(minutes=30)
CANONICAL_EVENT = "analytics_request_completed"
KNOWN_REQUEST_KINDS = frozenset(
    {"analytics", "capability_preflight", "discovery", "unclassified"}
)
KNOWN_RESOLUTIONS = ("supported", "needs_clarification", "unsupported")
KNOWN_STATUSES = (
    "success",
    "failure",
    "denied",
    "unsupported",
    "needs_clarification",
)
_USER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TRAFFIC_METRIC_IDS = frozenset(metric["metric_id"] for metric in TRAFFIC_METRICS)
_TRAFFIC_DIMENSION_IDS = frozenset({"session_date"})
_CATALOG_METRIC_IDS = frozenset(
    metric_id
    for profile in semantic_catalog.profiles.values()
    for metric_id, metric in profile["metrics"].items()
    if metric.get("status") == "published"
)
_CATALOG_DIMENSION_IDS = frozenset(semantic_catalog.dimensions)


class KPIInputError(ValueError):
    """Raised when a dashboard request has an unsafe or ambiguous boundary."""


def _parse_datetime(value: Any, *, name: str = "datetime") -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError as error:
            raise KPIInputError(f"{name} must be an ISO-8601 timestamp") from error
    else:
        raise KPIInputError(f"{name} must be a timezone-aware datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise KPIInputError(f"{name} requires a timezone")
    return result.astimezone(timezone.utc)


def _parse_date(value: Any, *, name: str) -> date:
    if isinstance(value, datetime):
        return _parse_datetime(value, name=name).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise KPIInputError(f"{name} must be an ISO date") from error
    raise KPIInputError(f"{name} must be an ISO date")


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError) as error:
        raise KPIInputError(f"unknown KPI timezone: {name}") from error


def _value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _raw_event(event: Any) -> dict[str, Any] | None:
    if isinstance(event, UsageEvent):
        return event.wire_dict()
    if isinstance(event, Mapping):
        return dict(event)
    return None


def _validated_raw_event(event: Any) -> dict[str, Any] | None:
    """Return a normalized v1 event only when the full wire contract passes.

    BigQuery rows and test fixtures arrive as dictionaries, so checking only
    the deduplication envelope would allow arbitrary enum values or malformed
    arrays to influence a dashboard.  Reusing the canonical Pydantic model
    keeps this offline aggregator fail-closed with the emitter contract.
    """
    try:
        raw = _raw_event(event)
    except Exception:
        return None
    if raw is None or _synthetic_event(raw):
        return None
    if _canonical_key(raw) is None:
        return None
    try:
        # ``UsageEvent`` intentionally uses strict datetime validation.  The
        # wire contract carries ISO text, while a BigQuery row may already
        # contain a datetime object, so normalize this one field before the
        # model performs the remaining strict checks.
        normalized = dict(raw)
        normalized["event_time"] = _parse_datetime(raw.get("event_time"), name="event_time")
        return UsageEvent.model_validate(normalized).wire_dict()
    except Exception:
        return None


def _canonical_key(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
    if event.get("schema_version") != "1.0":
        return None
    if event.get("event_name") != CANONICAL_EVENT:
        return None
    interaction_id = event.get("interaction_id")
    if not isinstance(interaction_id, str) or len(interaction_id) != 36:
        return None
    try:
        if str(UUID(interaction_id)) != interaction_id:
            return None
    except (ValueError, AttributeError):
        return None
    return ("1.0", interaction_id, CANONICAL_EVENT)


def _synthetic_event(event: Mapping[str, Any]) -> bool:
    """Recognize the marker used by the routing probe without reading payloads."""
    labels = event.get("labels")
    if isinstance(labels, Mapping):
        return labels.get("usage_validation") == "true"
    return event.get("usage_validation") is True


@dataclass(frozen=True)
class _PreparedEvent:
    values: Mapping[str, Any]
    event_time: datetime

    @property
    def interaction_id(self) -> str:
        return str(self.values["interaction_id"])

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _prepare_events(events: Iterable[Any]) -> tuple[list[_PreparedEvent], dict[str, int]]:
    """Validate the safe event envelope and deduplicate canonical records."""
    if events is None:
        events = ()
    prepared: list[_PreparedEvent] = []
    seen: set[tuple[str, str, str]] = set()
    quality = {
        "input_events": 0,
        "invalid_events_dropped": 0,
        "duplicate_events_removed": 0,
        "synthetic_events_dropped": 0,
    }
    for item in events:
        quality["input_events"] += 1
        try:
            raw = _raw_event(item)
        except Exception:
            raw = None
        if raw is None:
            quality["invalid_events_dropped"] += 1
            continue
        if _synthetic_event(raw):
            quality["synthetic_events_dropped"] += 1
            continue
        validated = _validated_raw_event(raw)
        if validated is None:
            quality["invalid_events_dropped"] += 1
            continue
        raw = validated
        key = _canonical_key(raw)
        if key is None:
            # Attachments, discovery records from another schema, and malformed
            # inputs must never silently become KPI events.
            quality["invalid_events_dropped"] += 1
            continue
        if key in seen:
            quality["duplicate_events_removed"] += 1
            continue
        try:
            event_time = _parse_datetime(raw.get("event_time"), name="event_time")
        except KPIInputError:
            quality["invalid_events_dropped"] += 1
            continue
        seen.add(key)
        prepared.append(_PreparedEvent(raw, event_time))
    prepared.sort(key=lambda event: (event.event_time, event.interaction_id))
    quality["deduplicated_events"] = len(prepared)
    return prepared, quality


def event_identity(event: Any) -> tuple[str, str, str] | None:
    """Return the canonical deduplication identity, or ``None`` if unsafe."""
    raw = _validated_raw_event(event)
    return _canonical_key(raw) if raw is not None else None


def deduplicate_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Return canonical, non-synthetic events once per wire identity."""
    prepared, _quality = _prepare_events(events)
    return [dict(event.values) for event in prepared]


def _local_date(event: _PreparedEvent, zone: ZoneInfo) -> date:
    return event.event_time.astimezone(zone).date()


def _local_datetime(event: _PreparedEvent, zone: ZoneInfo) -> datetime:
    return event.event_time.astimezone(zone)


def _user_id(event: _PreparedEvent) -> str | None:
    if event.get("identity_status") != "verified":
        return None
    user_id = event.get("user_id")
    if not isinstance(user_id, str) or _USER_ID_RE.fullmatch(user_id) is None:
        return None
    return user_id


def _valid_code(value: Any) -> str | None:
    return value if isinstance(value, str) and _CODE_RE.fullmatch(value) else None


def _period_events(events: Iterable[_PreparedEvent], start: date, end: date, zone: ZoneInfo) -> list[_PreparedEvent]:
    return [event for event in events if start <= _local_date(event, zone) <= end]


def _success_analytics(events: Iterable[_PreparedEvent]) -> list[_PreparedEvent]:
    return [
        event
        for event in events
        if event.get("request_kind") == "analytics"
        and event.get("status") == "success"
        and _user_id(event) is not None
    ]


def _safe_rate(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _funnel_rate(
    numerator_users: set[str] | None,
    denominator_users: set[str] | None,
    *,
    name: str,
) -> tuple[float | None, str | None]:
    """Return a rate only when the two funnel stages form a staircase."""
    if numerator_users is None or denominator_users is None:
        return None, f"{name}_stage_unavailable"
    if not denominator_users:
        return None, f"{name}_denominator_empty"
    if not numerator_users.issubset(denominator_users):
        return None, f"{name}_stages_not_comparable"
    return _safe_rate(len(numerator_users), len(denominator_users)), None


def _percentile(values: Iterable[int | float], percentile: float) -> float | None:
    numbers = sorted(float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0)
    if not numbers:
        return None
    if len(numbers) == 1:
        return int(numbers[0]) if numbers[0].is_integer() else round(numbers[0], 2)
    position = (len(numbers) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    value = numbers[lower] if lower == upper else numbers[lower] + (numbers[upper] - numbers[lower]) * (position - lower)
    return int(value) if value.is_integer() else round(value, 2)


def _count_series(events: Iterable[_PreparedEvent], zone: ZoneInfo, grain: str) -> dict[str, int]:
    users_by_bucket: dict[str, set[str]] = defaultdict(set)
    for event in events:
        user_id = _user_id(event)
        if user_id is None or event.get("request_kind") != "analytics" or event.get("status") != "success":
            continue
        local = _local_datetime(event, zone)
        if grain == "day":
            bucket = local.date().isoformat()
        elif grain == "week":
            bucket = (local.date() - timedelta(days=local.weekday())).isoformat()
        elif grain == "month":
            bucket = local.date().replace(day=1).isoformat()
        else:
            raise KPIInputError(f"unknown active-user grain: {grain}")
        users_by_bucket[bucket].add(user_id)
    return {bucket: len(users) for bucket, users in sorted(users_by_bucket.items())}


def _series_summary(series: Mapping[str, int]) -> dict[str, Any]:
    values = list(series.values())
    return {
        "series": dict(series),
        "average": round(sum(values) / len(values), 6) if values else None,
        "peak": max(values) if values else 0,
        "buckets": len(values),
    }


def _distribution(
    events: Iterable[_PreparedEvent],
    *,
    field: str,
    request_kinds: tuple[str, ...] = ("analytics",),
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, Counter[str]] = {kind: Counter() for kind in request_kinds}
    for event in events:
        kind = event.get("request_kind")
        if kind not in grouped:
            continue
        value = event.get(field)
        if isinstance(value, str) and value:
            grouped[kind][value] += 1
    return {
        kind: [{"value": value, "count": count} for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]
        for kind, counter in grouped.items()
    }


def _array_distribution(events: Iterable[_PreparedEvent], field: str) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for event in events:
        if event.get("request_kind") != "analytics":
            continue
        values = event.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        # One request contributes at most once per validated ID, independent
        # of result row count or duplicate array values.
        valid = {_valid_code(value) for value in values}
        if event.get("tool_name") == "traffic_summary":
            allowed = _TRAFFIC_METRIC_IDS if field == "metrics" else _TRAFFIC_DIMENSION_IDS
            valid = {value for value in valid if value in allowed}
        elif event.get("tool_name") == "query_ga4":
            allowed = _CATALOG_METRIC_IDS if field == "metrics" else _CATALOG_DIMENSION_IDS
            valid = {value for value in valid if value in allowed}
        else:
            # Arrays from other tools are not a validated analytics demand
            # mapping.  Keep their events in request/quality KPIs, but do not
            # promote arbitrary strings into top metric/dimension demand.
            valid = set()
        counts.update(value for value in valid if value is not None)
    return [{"value": value, "count": count} for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _period_distribution(events: Iterable[_PreparedEvent]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str, Any, str]] = Counter()
    for event in events:
        kind = event.get("request_kind")
        if kind not in ("analytics", "capability_preflight"):
            continue
        period_type = event.get("period_type") if isinstance(event.get("period_type"), str) else "unknown"
        requested_days = event.get("requested_days")
        if type(requested_days) is not int or requested_days <= 0:
            requested_days = None
        comparison = event.get("comparison_type") if isinstance(event.get("comparison_type"), str) else "unknown"
        counts[(kind, period_type, requested_days, comparison)] += 1
    return [
        {"request_kind": kind, "period_type": period_type, "requested_days": days, "comparison_type": comparison, "count": count}
        for (kind, period_type, days, comparison), count in sorted(
            counts.items(),
            key=lambda item: (item[0][0], item[0][1], item[0][2] is None, item[0][2] or 0, item[0][3]),
        )
    ]


def _status_counts(events: Iterable[_PreparedEvent]) -> dict[str, int]:
    counter = Counter(event.get("status") for event in events if event.get("status") in KNOWN_STATUSES)
    return {status: counter.get(status, 0) for status in KNOWN_STATUSES}


def _status_counts_by_kind(events: Iterable[_PreparedEvent]) -> dict[str, dict[str, int]]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        kind = event.get("request_kind")
        status = event.get("status")
        if kind in KNOWN_REQUEST_KINDS and status in KNOWN_STATUSES:
            grouped[kind][status] += 1
    return {
        kind: {status: counter.get(status, 0) for status in KNOWN_STATUSES}
        for kind, counter in sorted(grouped.items())
    }


def _resolution_report(events: Iterable[_PreparedEvent]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in ("capability_preflight", "analytics"):
        counter = Counter(event.get("resolution") for event in events if event.get("request_kind") == kind)
        known = sum(counter.get(value, 0) for value in KNOWN_RESOLUTIONS)
        result[kind] = {
            "counts": {value: counter.get(value, 0) for value in KNOWN_RESOLUTIONS},
            "known_total": known,
            "unknown_count": counter.get(None, 0) + counter.get("unknown", 0),
            "rates": {value: _safe_rate(counter.get(value, 0), known) for value in KNOWN_RESOLUTIONS},
        }
        total = sum(counter.values())
        result[kind]["unknown_rate"] = _safe_rate(result[kind]["unknown_count"], total)
    return result


def _reason_distribution(events: Iterable[_PreparedEvent]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for kind in ("analytics", "capability_preflight"):
        counts = Counter(
            event.get("unsupported_reason")
            for event in events
            if event.get("request_kind") == kind
            and event.get("status") == "unsupported"
            and isinstance(event.get("unsupported_reason"), str)
        )
        result[kind] = [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
    return result


def _latency_breakdown(events: Iterable[_PreparedEvent], field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for event in events:
        value = event.get("latency_ms")
        if type(value) is not int or value < 0:
            continue
        key = event.get(field)
        if isinstance(key, str) and key:
            grouped[key].append(value)
    return {
        key: {"count": len(values), "p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95)}
        for key, values in sorted(grouped.items())
    }


def _error_breakdown(events: Iterable[_PreparedEvent], field: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        code = event.get("error_code")
        key = event.get(field)
        if isinstance(key, str) and key and isinstance(code, str) and code:
            grouped[key][code] += 1
    return {
        key: [
            {"value": value, "count": count}
            for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        ]
        for key, counter in sorted(grouped.items())
    }


def _session_count(events: Iterable[_PreparedEvent], zone: ZoneInfo) -> int:
    grouped: dict[str, list[_PreparedEvent]] = defaultdict(list)
    for event in events:
        user_id = _user_id(event)
        if user_id is not None:
            grouped[user_id].append(event)
    total = 0
    for user_events in grouped.values():
        ordered = sorted(user_events, key=lambda event: event.event_time)
        if not ordered:
            continue
        total += 1
        for previous, current in zip(ordered, ordered[1:]):
            if current.event_time - previous.event_time > SESSION_GAP:
                total += 1
    return total


def _user_activity(events: Iterable[_PreparedEvent], zone: ZoneInfo) -> dict[str, Any]:
    dates: dict[str, set[date]] = defaultdict(set)
    requests: Counter[str] = Counter()
    for event in events:
        if event.get("request_kind") != "analytics":
            continue
        user_id = _user_id(event)
        if user_id is None:
            continue
        # Requests-per-user counts every analytics attempt, while active-day
        # and repeat-usage metrics intentionally require a successful query.
        requests[user_id] += 1
        if event.get("status") == "success":
            dates[user_id].add(_local_date(event, zone))
    active_days = list(len(days) for days in dates.values())
    request_counts = list(requests.values())
    return {
        "active_days_per_user": {
            "user_count": len(active_days),
            "average": round(sum(active_days) / len(active_days), 6) if active_days else None,
            "distribution": {str(value): active_days.count(value) for value in sorted(set(active_days))},
        },
        "analytics_requests_per_user": {
            "user_count": len(request_counts),
            "average": round(sum(request_counts) / len(request_counts), 6) if request_counts else None,
            "distribution": {str(value): request_counts.count(value) for value in sorted(set(request_counts))},
        },
        "users_with_two_active_days": sum(1 for value in active_days if value >= 2),
        "active_users": len(active_days),
        "repeat_usage_rate": _safe_rate(sum(1 for value in active_days if value >= 2), len(active_days)),
    }


def _history_date(value: Any, zone: ZoneInfo) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    try:
        return _parse_datetime(value, name="history boundary").astimezone(zone).date()
    except KPIInputError:
        return None


def _combine_identity_continuity(*values: bool | None) -> bool | None:
    """Combine attestations while preserving known negative evidence."""
    if any(value is False for value in values):
        return False
    if any(value is True for value in values):
        return True
    return None


@dataclass(frozen=True)
class HistoryCoverage:
    """Evidence needed before publishing long-lived activation/cohort KPIs."""

    measurement_version: str | None = None
    measurement_start: date | None = None
    measurement_end: date | None = None
    event_history_start: date | None = None
    event_history_end: date | None = None
    ledger_available: bool = False
    ledger_policy_approved: bool = False
    # Continuity is an attestation, not a safe default.  A missing value must
    # degrade long-lived activation and cohort metrics until the pipeline
    # positively confirms that the measurement version survived key rotation.
    identity_continuous: bool | None = None
    pipeline_complete: bool = False
    ledger_deleted: bool = False
    extra_reasons: tuple[str, ...] = ()

    def evaluate(self, *, report_end: date, ledger_version: str | None = None) -> dict[str, Any]:
        reasons = list(self.extra_reasons)
        if self.measurement_version is None:
            reasons.append("measurement_version_missing")
        elif _valid_code(self.measurement_version) is None:
            reasons.append("measurement_version_invalid")
        elif ledger_version is not None and self.measurement_version != ledger_version:
            reasons.append("measurement_version_mismatch")
        if self.measurement_start is None:
            reasons.append("measurement_start_missing")
        if self.measurement_end is None:
            reasons.append("measurement_end_missing")
        if self.measurement_end is not None and report_end >= self.measurement_end:
            reasons.append("measurement_window_expired")
        if self.ledger_available is not True:
            reasons.append("ledger_unavailable")
        if self.ledger_policy_approved is not True:
            reasons.append("ledger_policy_unapproved")
        if not isinstance(self.ledger_deleted, bool):
            reasons.append("ledger_deletion_status_invalid")
        elif self.ledger_deleted is True:
            reasons.append("ledger_deleted_or_expired")
        if self.identity_continuous is False:
            reasons.append("identity_continuity_break")
        elif self.identity_continuous is not True:
            reasons.append("identity_continuity_unverified")
        if self.pipeline_complete is not True:
            reasons.append("pipeline_gap_or_watermark_unknown")
        elif self.event_history_start is None or self.event_history_end is None:
            reasons.append("event_history_bounds_missing")
        if self.measurement_start is not None and self.event_history_start is not None and self.event_history_start > self.measurement_start:
            reasons.append("event_history_starts_after_measurement")
        if self.event_history_end is not None and self.event_history_end < report_end:
            reasons.append("event_history_ends_before_report")
        reasons = list(dict.fromkeys(reasons))
        status = "complete" if not reasons else "degraded"
        return {
            "status": status,
            "measurement_version": ledger_version,
            "measurement_start": _iso_date(self.measurement_start),
            "measurement_end": _iso_date(self.measurement_end),
            "event_history_start": _iso_date(self.event_history_start),
            "event_history_end": _iso_date(self.event_history_end),
            "reasons": reasons,
            "can_publish_cumulative": status == "complete",
            "can_publish_cohort": status == "complete",
        }


def _add_calendar_year(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        # The policy is a calendar anniversary; Feb 29 rolls to Feb 28 in a
        # non-leap year rather than silently becoming 365 days.
        return value.replace(year=value.year + 1, day=28)


@dataclass(frozen=True)
class ActivationRecord:
    user_id: str
    first_success_at: datetime
    measurement_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "first_success_at": _iso_datetime(self.first_success_at) or "",
            "measurement_version": self.measurement_version,
        }


class ActivationLedger:
    """Small idempotent first-success ledger for activation and cohort views.

    Only the pseudonymous ID, earliest success timestamp and measurement
    version are retained.  A ledger can be updated by a background pipeline;
    failures in that pipeline do not need to be raised into an analytics
    request.
    """

    def __init__(
        self,
        *,
        measurement_start: Any = None,
        measurement_version: str = DEFAULT_MEASUREMENT_VERSION,
        retention_end: Any = None,
        policy_approved: bool = False,
        identity_continuous: bool | None = None,
    ) -> None:
        if not isinstance(measurement_version, str) or _CODE_RE.fullmatch(measurement_version) is None:
            raise KPIInputError("measurement_version must be a bounded code")
        self.measurement_version = measurement_version
        self.measurement_start = _parse_datetime(measurement_start, name="measurement_start") if measurement_start is not None else None
        self.retention_end = _parse_datetime(retention_end, name="retention_end") if retention_end is not None else (
            _add_calendar_year(self.measurement_start) if self.measurement_start is not None else None
        )
        self.policy_approved = policy_approved is True
        self.records: dict[str, ActivationRecord] = {}
        self.pipeline_gap = False
        if identity_continuous is not None and not isinstance(identity_continuous, bool):
            raise KPIInputError("identity_continuous must be a boolean attestation")
        self.identity_continuous = identity_continuous
        self.deleted_or_expired = False

    def _eligible_event(self, event: _PreparedEvent) -> bool:
        user_id = _user_id(event)
        if user_id is None or event.get("request_kind") != "analytics" or event.get("status") != "success":
            return False
        if self.measurement_start is not None and event.event_time < self.measurement_start:
            return False
        if self.retention_end is not None and event.event_time >= self.retention_end:
            return False
        return True

    def apply(self, events: Iterable[Any], *, as_of: Any = None) -> dict[str, int | str]:
        """Idempotently incorporate successful verified analytics events.

        The returned counters are safe diagnostics and can be persisted by the
        pipeline; no event payload or tenant data is retained in the ledger.
        """
        prepared, quality = _prepare_events(events)
        if quality["invalid_events_dropped"]:
            # A rejected canonical source row might be an unrecorded first
            # success.  Do not partially apply the batch or claim continuity.
            self.mark_pipeline_gap()
            raise KPIInputError("ledger batch contains invalid canonical events")
        if as_of is not None:
            cutoff = _parse_datetime(as_of, name="as_of")
        else:
            cutoff = max((event.event_time for event in prepared), default=datetime.now(timezone.utc))
        result: dict[str, Any] = {
            "processed_events": len(prepared),
            "eligible_events": 0,
            "inserted_users": 0,
            "backdated_users": 0,
            "unchanged_users": 0,
            "ignored_events": quality["invalid_events_dropped"] + quality["synthetic_events_dropped"],
            "deduplicated_events": quality["duplicate_events_removed"],
            "measurement_version": self.measurement_version,
        }
        for event in prepared:
            if event.event_time > cutoff or not self._eligible_event(event):
                result["ignored_events"] += 1
                continue
            result["eligible_events"] += 1
            user_id = _user_id(event)
            assert user_id is not None
            existing = self.records.get(user_id)
            if existing is None:
                self.records[user_id] = ActivationRecord(user_id, event.event_time, self.measurement_version)
                result["inserted_users"] += 1
            elif event.event_time < existing.first_success_at:
                self.records[user_id] = ActivationRecord(user_id, event.event_time, self.measurement_version)
                result["backdated_users"] += 1
            else:
                result["unchanged_users"] += 1
        return result

    def mark_pipeline_gap(self) -> None:
        self.pipeline_gap = True

    def mark_identity_break(self) -> None:
        self.identity_continuous = False

    def delete_user(self, user_id: str) -> bool:
        if not isinstance(user_id, str) or _USER_ID_RE.fullmatch(user_id) is None:
            return False
        removed = self.records.pop(user_id, None) is not None
        if removed:
            self.deleted_or_expired = True
        return removed

    def expire(self) -> None:
        self.records.clear()
        self.deleted_or_expired = True

    def history_coverage(
        self,
        *,
        event_history_start: Any = None,
        event_history_end: Any = None,
        pipeline_complete: bool | None = None,
        identity_continuous: bool | None = None,
    ) -> HistoryCoverage:
        combined_identity = _combine_identity_continuity(
            self.identity_continuous,
            identity_continuous,
        )
        return HistoryCoverage(
            measurement_version=self.measurement_version,
            measurement_start=self.measurement_start.date() if self.measurement_start else None,
            measurement_end=self.retention_end.date() if self.retention_end else None,
            event_history_start=_history_date(event_history_start, ZoneInfo("UTC")),
            event_history_end=_history_date(event_history_end, ZoneInfo("UTC")),
            ledger_available=True,
            ledger_policy_approved=self.policy_approved,
            identity_continuous=combined_identity,
            # A known gap cannot be overridden by a later positive argument;
            # otherwise completeness must be positively attested.
            pipeline_complete=not self.pipeline_gap and pipeline_complete is True,
            ledger_deleted=self.deleted_or_expired,
        )

    def snapshot(self) -> tuple[ActivationRecord, ...]:
        return tuple(sorted(self.records.values(), key=lambda record: (record.first_success_at, record.user_id)))


def update_activation_ledger(
    ledger: ActivationLedger,
    events: Iterable[Any],
    *,
    as_of: Any = None,
) -> dict[str, Any]:
    """Failure-isolated background update entry point.

    A malformed batch, source outage, or timestamp failure marks a pipeline
    gap and returns a fixed diagnostic code.  It never exposes an exception or
    changes the outcome of the analytics request that produced the events.
    """
    try:
        result = ledger.apply(events, as_of=as_of)
        result["status"] = "ok"
        return result
    except Exception:
        try:
            ledger.mark_pipeline_gap()
        except Exception:
            pass
        return {
            "status": "failed",
            "error_code": "ledger_update_failed",
            "measurement_version": getattr(ledger, "measurement_version", None),
        }


def _history_from_input(
    supplied: HistoryCoverage | Mapping[str, Any] | None,
    *,
    ledger: ActivationLedger | None,
    measurement_start: Any,
    measurement_version: str,
    event_history_start: Any,
    event_history_end: Any,
    history_complete: bool,
    ledger_policy_approved: bool,
    zone: ZoneInfo,
) -> HistoryCoverage:
    if isinstance(supplied, HistoryCoverage):
        coverage = supplied
        if ledger is not None:
            measurement_start_date = ledger.measurement_start.astimezone(zone).date() if ledger.measurement_start else coverage.measurement_start
            measurement_end_date = ledger.retention_end.astimezone(zone).date() if ledger.retention_end else coverage.measurement_end
            mismatch = []
            if coverage.measurement_start is not None and measurement_start_date is not None and coverage.measurement_start != measurement_start_date:
                mismatch.append("measurement_metadata_mismatch")
            if not isinstance(coverage.ledger_deleted, bool):
                mismatch.append("ledger_deletion_status_invalid")
            coverage = replace(
                coverage,
                measurement_start=measurement_start_date,
                measurement_end=measurement_end_date,
                ledger_available=True,
                ledger_policy_approved=coverage.ledger_policy_approved is True and ledger.policy_approved,
                identity_continuous=_combine_identity_continuity(
                    coverage.identity_continuous,
                    ledger.identity_continuous,
                ),
                pipeline_complete=coverage.pipeline_complete is True and not ledger.pipeline_gap,
                ledger_deleted=coverage.ledger_deleted is True or ledger.deleted_or_expired,
                extra_reasons=tuple(dict.fromkeys((*coverage.extra_reasons, *mismatch))),
            )
        return coverage
    if isinstance(supplied, Mapping):
        coverage = HistoryCoverage(
            measurement_version=_valid_code(supplied.get("measurement_version")),
            measurement_start=_history_date(supplied.get("measurement_start"), zone),
            measurement_end=_history_date(supplied.get("measurement_end"), zone),
            event_history_start=_history_date(supplied.get("event_history_start"), zone),
            event_history_end=_history_date(supplied.get("event_history_end"), zone),
            ledger_available=supplied.get("ledger_available") is True,
            ledger_policy_approved=supplied.get("ledger_policy_approved") is True,
            # Only literal booleans are evidence. Missing, null and truthy
            # strings remain unknown and fail closed.
            identity_continuous=(
                supplied.get("identity_continuous")
                if isinstance(supplied.get("identity_continuous"), bool)
                else None
            ),
            pipeline_complete=supplied.get("pipeline_complete") is True,
            ledger_deleted=supplied.get("ledger_deleted", False),
            extra_reasons=tuple(value for value in supplied.get("reasons", ()) if isinstance(value, str)),
        )
        if ledger is not None:
            measurement_start_date = ledger.measurement_start.astimezone(zone).date() if ledger.measurement_start else coverage.measurement_start
            measurement_end_date = ledger.retention_end.astimezone(zone).date() if ledger.retention_end else coverage.measurement_end
            mismatch = []
            if coverage.measurement_start is not None and measurement_start_date is not None and coverage.measurement_start != measurement_start_date:
                mismatch.append("measurement_metadata_mismatch")
            if not isinstance(coverage.ledger_deleted, bool):
                mismatch.append("ledger_deletion_status_invalid")
            coverage = replace(
                coverage,
                measurement_start=measurement_start_date,
                measurement_end=measurement_end_date,
                ledger_available=True,
                ledger_policy_approved=coverage.ledger_policy_approved is True and ledger.policy_approved,
                identity_continuous=_combine_identity_continuity(
                    coverage.identity_continuous,
                    ledger.identity_continuous,
                ),
                pipeline_complete=coverage.pipeline_complete is True and not ledger.pipeline_gap,
                ledger_deleted=coverage.ledger_deleted is True or ledger.deleted_or_expired,
                extra_reasons=tuple(dict.fromkeys((*coverage.extra_reasons, *mismatch))),
            )
        return coverage
    if ledger is not None:
        return ledger.history_coverage(
            event_history_start=event_history_start,
            event_history_end=event_history_end,
            # A ledger row by itself is not evidence that the canonical event
            # window and pipeline watermark are complete.  The dashboard job
            # must explicitly attest that fact.
            pipeline_complete=history_complete,
        )
    return HistoryCoverage(
        measurement_version=_valid_code(measurement_version),
        measurement_start=_history_date(measurement_start, zone),
        event_history_start=_history_date(event_history_start, zone),
        event_history_end=_history_date(event_history_end, zone),
        ledger_available=False,
        ledger_policy_approved=ledger_policy_approved is True,
        pipeline_complete=history_complete is True,
    )


def _funnel(
    events: list[_PreparedEvent],
    *,
    eligible_users: set[str] | None,
    authorized_users: set[str] | None,
) -> dict[str, Any]:
    tried_users: set[str] = set()
    activated_users: set[str] = set()
    for event in events:
        user_id = _user_id(event)
        if user_id is None:
            continue
        if (
            event.get("transport") == "mcp"
            and isinstance(event.get("tool_name"), str)
            and event.get("request_kind") in KNOWN_REQUEST_KINDS - {"unclassified"}
            and event.get("error_code") != "invalid_schema"
            and event.get("status") != "denied"
            and event.get("authorization_result") != "denied"
        ):
            tried_users.add(user_id)
        if event.get("request_kind") == "analytics" and event.get("status") == "success":
            activated_users.add(user_id)

    def stage(count: int | None, reason: str | None = None) -> dict[str, Any]:
        return {"count": count, "available": count is not None, **({"reason": reason} if reason else {})}

    eligible_count = len(eligible_users) if eligible_users is not None else None
    authorized_count = len(authorized_users) if authorized_users is not None else None
    conversion = {}
    conversion_reasons = []
    for name, numerator, denominator in (
        ("authorized_over_eligible", authorized_users, eligible_users),
        ("tried_over_authorized", tried_users, authorized_users),
        ("activated_over_tried", activated_users, tried_users),
    ):
        rate, reason = _funnel_rate(numerator, denominator, name=name)
        conversion[name] = rate
        if reason is not None:
            conversion_reasons.append(f"{name}:{reason}")
    # REST activation is observable without an MCP tool-call stage.  In that
    # mixed-transport case, and for any other non-staircase input, the ratio
    # could exceed 100% and would be misleading (and invalid for the versioned
    # rate contract).  Keep the stage counts and record why the rate is null.
    transport_note = (
        "REST activation can exist without an MCP tried stage; funnel rates are "
        "published only when each numerator user set is a subset of its denominator "
        "stage."
    )
    if conversion_reasons:
        transport_note += " Unavailable conversion rates: " + ", ".join(conversion_reasons) + "."
    return {
        "eligible": stage(eligible_count, None if eligible_users is not None else "external_denominator_unavailable"),
        "authorized": stage(authorized_count, None if authorized_users is not None else "connection_ledger_unavailable"),
        "tried": stage(len(tried_users)),
        "activated": stage(len(activated_users)),
        "conversion_rates": conversion,
        "users": {
            "tried": len(tried_users),
            "activated": len(activated_users),
        },
        "transport_note": transport_note,
    }


def _safe_user_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {
        value
        for value in values
        if isinstance(value, str) and _USER_ID_RE.fullmatch(value) is not None
    }


def _retention(
    all_events: list[_PreparedEvent],
    period_events: list[_PreparedEvent],
    *,
    ledger: ActivationLedger | None,
    coverage: dict[str, Any],
    period_start: date,
    period_end: date,
    report_as_of: datetime,
    zone: ZoneInfo,
) -> dict[str, Any]:
    if ledger is None:
        return {"status": "unavailable", "reason": "activation_ledger_unavailable", "w0_users": None, "mature_users": None, "retained_users": None, "rate": None}
    if not coverage.get("can_publish_cohort"):
        return {"status": "insufficient_history", "reason": "history_coverage_insufficient", "w0_users": None, "mature_users": None, "retained_users": None, "rate": None}
    report_local = report_as_of.astimezone(zone).date()
    records = [
        record
        for record in ledger.snapshot()
        if record.first_success_at <= report_as_of
        and period_start <= record.first_success_at.astimezone(zone).date() - timedelta(days=record.first_success_at.astimezone(zone).weekday()) <= period_end
    ]
    if not records:
        return {"status": "no_cohort", "reason": None, "w0_users": 0, "mature_users": 0, "retained_users": 0, "rate": None}
    history_start = _parse_date_or_none(coverage.get("event_history_start"))
    history_end = _parse_date_or_none(coverage.get("event_history_end"))
    measurement_end = _parse_date_or_none(coverage.get("measurement_end"))
    mature: list[ActivationRecord] = []
    immature = 0
    uncovered = 0
    for record in records:
        w0 = record.first_success_at.astimezone(zone).date() - timedelta(days=record.first_success_at.astimezone(zone).weekday())
        w4_end = w0 + timedelta(days=34)
        if report_local < w4_end:
            immature += 1
        elif (
            history_start is None
            or history_end is None
            or measurement_end is None
            or history_start > w0
            or history_end < w4_end
            or w4_end >= measurement_end
        ):
            uncovered += 1
        else:
            mature.append(record)
    if not mature:
        if uncovered:
            return {
                "status": "insufficient_history",
                "reason": "follow_up_window_not_covered",
                "w0_users": len(records),
                "mature_users": 0,
                "immature_users": immature + uncovered,
                "retained_users": None,
                "rate": None,
            }
        return {"status": "not_mature", "reason": "w4_window_not_complete", "w0_users": len(records), "mature_users": 0, "immature_users": immature, "retained_users": None, "rate": None}
    retained: set[str] = set()
    for event in _success_analytics(all_events):
        user_id = _user_id(event)
        if user_id is None:
            continue
        local_date = _local_date(event, zone)
        for record in mature:
            w0 = record.first_success_at.astimezone(zone).date() - timedelta(days=record.first_success_at.astimezone(zone).weekday())
            if record.user_id == user_id and w0 + timedelta(days=28) <= local_date <= w0 + timedelta(days=34):
                retained.add(user_id)
    return {"status": "available", "reason": None, "w0_users": len(records), "mature_users": len(mature), "immature_users": immature + uncovered, "retained_users": len(retained), "rate": _safe_rate(len(retained), len(mature))}


def _parse_date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return _parse_date(value, name="history boundary")
    except KPIInputError:
        return None


def build_kpi_view(
    events: Iterable[Any],
    period_start: Any = None,
    period_end: Any = None,
    *,
    start_date: Any = None,
    end_date: Any = None,
    as_of: Any = None,
    generated_at: Any = None,
    timezone_name: str = DEFAULT_TIME_ZONE,
    ledger: ActivationLedger | None = None,
    eligible_users: Iterable[str] | None = None,
    authorized_users: Iterable[str] | None = None,
    history: HistoryCoverage | Mapping[str, Any] | None = None,
    measurement_start: Any = None,
    measurement_version: str = DEFAULT_MEASUREMENT_VERSION,
    event_history_start: Any = None,
    event_history_end: Any = None,
    history_complete: bool = False,
    ledger_policy_approved: bool = False,
) -> dict[str, Any]:
    """Build the bounded Phase 11.6 dashboard contract.

    ``eligible_users`` and ``authorized_users`` are deliberately optional:
    they are external denominators and must never be inferred from events.
    ``history_complete`` is an explicit pipeline watermark assertion; absence
    of that assertion degrades cumulative activation and retention.
    """
    if period_start is None:
        period_start = start_date
    if period_end is None:
        period_end = end_date
    if period_start is None or period_end is None:
        raise KPIInputError("period_start and period_end are required")
    start = _parse_date(period_start, name="period_start")
    end = _parse_date(period_end, name="period_end")
    if end < start:
        raise KPIInputError("period_end must not precede period_start")
    zone = _timezone(timezone_name)
    prepared, quality = _prepare_events(events)
    if as_of is None:
        as_of_dt = max((event.event_time for event in prepared), default=datetime.now(timezone.utc))
    else:
        as_of_dt = _parse_datetime(as_of, name="as_of")
    generated_dt = _parse_datetime(generated_at, name="generated_at") if generated_at is not None else as_of_dt
    visible_events = [event for event in prepared if event.event_time <= as_of_dt]
    period_events = _period_events(visible_events, start, end, zone)
    success_events = _success_analytics(period_events)
    successful_analytics_events = [
        event
        for event in period_events
        if event.get("request_kind") == "analytics" and event.get("status") == "success"
    ]
    success_users = {_user_id(event) for event in success_events}
    success_users.discard(None)
    coverage_obj = _history_from_input(
        history,
        ledger=ledger,
        measurement_start=measurement_start,
        measurement_version=measurement_version,
        event_history_start=event_history_start,
        event_history_end=event_history_end,
        history_complete=history_complete,
        ledger_policy_approved=ledger_policy_approved,
        zone=zone,
    )
    coverage = coverage_obj.evaluate(report_end=end, ledger_version=ledger.measurement_version if ledger else measurement_version)
    usage_activity = _user_activity(period_events, zone)
    status = _status_counts(period_events)
    analytics_events = [event for event in period_events if event.get("request_kind") == "analytics"]
    analytics_status = _status_counts(analytics_events)
    tool_calls = [event for event in period_events if event.get("transport") == "mcp" and isinstance(event.get("tool_name"), str) and event.get("tool_name") and event.get("request_kind") != "unclassified"]
    identity_total = len(period_events)
    verified_total = sum(1 for event in period_events if _user_id(event) is not None)
    unknown_intent = sum(1 for event in period_events if event.get("analysis_goal") == "unknown" or event.get("analysis_subject") == "unknown")
    unknown_host = sum(1 for event in period_events if event.get("host") == "other" or not isinstance(event.get("host"), str))
    latest = max((event.event_time for event in period_events), default=None)
    latest_lag = max(0, int((as_of_dt - latest).total_seconds())) if latest is not None else None

    if ledger is not None:
        ledger_records = ledger.snapshot()
        period_ledger_records = [record for record in ledger_records if record.first_success_at <= as_of_dt and start <= record.first_success_at.astimezone(zone).date() <= end]
        activation_status = "available" if coverage["can_publish_cumulative"] else "degraded"
        activation_new = len(period_ledger_records) if coverage["can_publish_cumulative"] else None
        activation_cumulative = sum(1 for record in ledger_records if record.first_success_at <= as_of_dt and record.first_success_at.astimezone(zone).date() <= end) if coverage["can_publish_cumulative"] else None
    else:
        ledger_records = ()
        period_ledger_records = []
        activation_status = "unavailable"
        activation_new = None
        activation_cumulative = None

    event_window_start = min((_local_date(event, zone) for event in visible_events), default=None)
    event_window_end = max((_local_date(event, zone) for event in visible_events), default=None)
    quality_report = {
        **quality,
        "period_events": len(period_events),
        "identity_coverage": {
            "verified_events": verified_total,
            "total_events": identity_total,
            "rate": _safe_rate(verified_total, identity_total),
            "unknown_user_events": identity_total - verified_total,
        },
        "unknown_identity_events": identity_total - verified_total,
        "unknown_intent_events": unknown_intent,
        "unknown_intent_rate": _safe_rate(unknown_intent, identity_total),
        "unknown_host_events": unknown_host,
        "unknown_host_rate": _safe_rate(unknown_host, identity_total),
        "status_counts": status,
        "status_counts_by_request_kind": _status_counts_by_kind(period_events),
        "resolution": _resolution_report(period_events),
        "analytics_status_counts": analytics_status,
        "failure_rate": _safe_rate(analytics_status["failure"], len(analytics_events)),
        "denied_rate": _safe_rate(analytics_status["denied"], len(analytics_events)),
        "latency_ms": {
            "count": sum(1 for event in period_events if type(event.get("latency_ms")) is int and event.get("latency_ms") >= 0),
            "p50": _percentile((event.get("latency_ms") for event in period_events), 0.5),
            "p95": _percentile((event.get("latency_ms") for event in period_events), 0.95),
            "by_tool": _latency_breakdown(period_events, "tool_name"),
            "by_transport": _latency_breakdown(period_events, "transport"),
        },
        "error_categories": [{"value": value, "count": count} for value, count in sorted(Counter(event.get("error_code") for event in period_events if isinstance(event.get("error_code"), str)).items(), key=lambda item: (-item[1], item[0]))],
        "error_categories_by_tool": _error_breakdown(period_events, "tool_name"),
        "error_categories_by_transport": _error_breakdown(period_events, "transport"),
        "error_categories_by_status": _error_breakdown(period_events, "status"),
        "event_window": {"start_date": _iso_date(event_window_start), "end_date": _iso_date(event_window_end)},
    }

    denominator_eligible = _safe_user_set(eligible_users)
    denominator_authorized = _safe_user_set(authorized_users)
    return {
        "schema_version": KPI_SCHEMA_VERSION,
        "report_type": "usage_kpi_dashboard",
        "report_schema_version": KPI_SCHEMA_VERSION,
        "period": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": timezone_name,
            "week_start": "monday",
        },
        "generated_at": _iso_datetime(generated_dt),
        "data_freshness": {
            "as_of": _iso_datetime(as_of_dt),
            "latest_event_time": _iso_datetime(latest),
            "latest_event_lag_seconds": latest_lag,
            "history": coverage,
        },
        "coverage": quality_report,
        "funnel": _funnel(period_events, eligible_users=denominator_eligible, authorized_users=denominator_authorized),
        "activation": {
            "status": activation_status,
            "observed_period_users": len(success_users),
            "new_users": activation_new,
            "cumulative_users": activation_cumulative,
            # This is a cumulative ledger-derived number, so do not expose it
            # while policy/history evidence is degraded.  The observed period
            # count above remains available from canonical events.
            "ledger_users": (
                sum(1 for record in ledger_records if record.first_success_at <= as_of_dt)
                if ledger is not None and coverage["can_publish_cumulative"]
                else None
            ),
            "measurement_version": ledger.measurement_version if ledger else measurement_version,
            "history_coverage": coverage,
        },
        "retention": _retention(
            visible_events,
            period_events,
            ledger=ledger,
            coverage=coverage,
            period_start=start,
            period_end=end,
            report_as_of=as_of_dt,
            zone=zone,
        ),
        "usage": {
            "tool_calls": len(tool_calls),
            "analytics_requests": len(analytics_events),
            "successful_requests": len(successful_analytics_events),
            "inferred_sessions": _session_count(period_events, zone),
            "successful_users": len(success_users),
            "dau": _series_summary(_count_series(period_events, zone, "day")),
            "wau": _series_summary(_count_series(period_events, zone, "week")),
            "mau": _series_summary(_count_series(period_events, zone, "month")),
            **usage_activity,
        },
        "demand": {
            "analysis_goals": _distribution(period_events, field="analysis_goal", request_kinds=("analytics", "capability_preflight")),
            "analysis_subjects": _distribution(period_events, field="analysis_subject", request_kinds=("analytics", "capability_preflight")),
            "intent_sources": _distribution(period_events, field="intent_source", request_kinds=("analytics", "capability_preflight")),
            "metrics": _array_distribution(period_events, "metrics"),
            "dimensions": _array_distribution(period_events, "dimensions"),
            "period_patterns": _period_distribution(period_events),
            "comparison_types": _distribution(period_events, field="comparison_type", request_kinds=("analytics", "capability_preflight")),
            "unsupported_reasons": _reason_distribution(period_events),
        },
    }


def build_weekly_summary(
    events: Iterable[Any],
    week_start: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a Monday–Sunday summary with the same source and KPI semantics."""
    start = _parse_date(week_start, name="week_start")
    start -= timedelta(days=start.weekday())
    end = start + timedelta(days=6)
    view = build_kpi_view(events, start, end, **kwargs)
    return {
        "schema_version": KPI_SCHEMA_VERSION,
        "report_type": "usage_weekly_summary",
        "report_schema_version": KPI_SCHEMA_VERSION,
        "week": view["period"],
        "generated_at": view["generated_at"],
        "data_freshness": view["data_freshness"],
        "history_coverage": view["activation"]["history_coverage"],
        "kpis": {
            "activated_users": view["activation"]["new_users"],
            "cumulative_activated_users": view["activation"]["cumulative_users"],
            "successful_requests": view["usage"]["successful_requests"],
            "analytics_requests": view["usage"]["analytics_requests"],
            "successful_users": view["usage"]["successful_users"],
            "repeat_usage_rate": view["usage"]["repeat_usage_rate"],
            "w4_retention_rate": view["retention"]["rate"],
            "failure_rate": view["coverage"]["failure_rate"],
            "identity_coverage": view["coverage"]["identity_coverage"],
        },
        "funnel": view["funnel"],
        "quality": {
            "status_counts": view["coverage"]["status_counts"],
            "status_counts_by_request_kind": view["coverage"]["status_counts_by_request_kind"],
            "resolution": view["coverage"]["resolution"],
            "latency_ms": view["coverage"]["latency_ms"],
            "error_categories": view["coverage"]["error_categories"],
            "error_categories_by_status": view["coverage"]["error_categories_by_status"],
            "unknown_intent_rate": view["coverage"]["unknown_intent_rate"],
            "unknown_host_rate": view["coverage"]["unknown_host_rate"],
        },
        "demand": view["demand"],
    }


# Names used by offline dashboard jobs and future callers; keep the aliases
# explicit so a view migration does not silently change the semantics.
build_dashboard = build_kpi_view
build_usage_kpi_view = build_kpi_view


__all__ = [
    "ActivationLedger",
    "ActivationRecord",
    "HistoryCoverage",
    "KPIInputError",
    "KPI_SCHEMA_VERSION",
    "build_dashboard",
    "build_kpi_view",
    "build_usage_kpi_view",
    "build_weekly_summary",
    "deduplicate_events",
    "event_identity",
    "update_activation_ledger",
]
