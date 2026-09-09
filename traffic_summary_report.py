from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import math
from typing import Any, Mapping, Sequence


REPORT_TYPE = "traffic_summary"
REPORT_SCHEMA_VERSION = "1.0.0"
REPORT_CONTRACT_ERROR_CODE = "invalid_report_contract"
REPORT_CONTRACT_ERROR_MESSAGE = "目前無法產生流量摘要報表，請稍後再試。"

TRAFFIC_METRICS = (
    {
        "metric_id": "total_sessions",
        "label": "Sessions",
        "unit": "count",
    },
    {
        "metric_id": "total_users",
        "label": "Users",
        "unit": "count",
    },
    {
        "metric_id": "new_users",
        "label": "New users",
        "unit": "count",
    },
    {
        "metric_id": "returning_users",
        "label": "Returning users",
        "unit": "count",
    },
)

_SERIES = (
    {
        "series_id": "current",
        "label": "Current period",
        "color": "#2563EB",
        "order": 0,
    },
    {
        "series_id": "previous",
        "label": "Previous period",
        "color": "#94A3B8",
        "order": 1,
    },
)


class TrafficSummaryReportError(RuntimeError):
    """The query result could not be represented by the public report contract."""

    def __init__(self, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(REPORT_CONTRACT_ERROR_MESSAGE)
        self.code = REPORT_CONTRACT_ERROR_CODE
        self.message = REPORT_CONTRACT_ERROR_MESSAGE
        self.details = details or {}

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _iso_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return date.fromisoformat(value).isoformat()
    raise ValueError(f"Expected date for traffic summary report, got {value!r}")


def _non_negative_integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Traffic summary {field_name} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
    ):
        parsed = int(value)
    else:
        raise ValueError(f"Traffic summary {field_name} must be a non-negative integer")
    if parsed < 0:
        raise ValueError(f"Traffic summary {field_name} must be a non-negative integer")
    return parsed


def _count(value: Any) -> int:
    return _non_negative_integer(value, field_name="count")


def _day_index(value: Any) -> int:
    return _non_negative_integer(value, field_name="day_index")


def _percentage(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Traffic summary percentage must be a finite number or null")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Traffic summary percentage must be a finite number or null")
    return parsed


def _metric_values(value: Any) -> dict[str, int]:
    return {
        metric["metric_id"]: _count(_field(value, metric["metric_id"]))
        for metric in TRAFFIC_METRICS
    }


def _date_basis() -> dict[str, str]:
    return {
        "type": "source_date",
        "source_field": "mar_ga_sessions.session_date",
        "source_data_type": "DATE",
        "time_zone_conversion": "none",
    }


def _presentation() -> dict[str, Any]:
    charts = []
    for order, metric in enumerate(TRAFFIC_METRICS):
        metric_id = metric["metric_id"]
        charts.append(
            {
                "chart_id": metric_id,
                "title": metric["label"],
                "unit": metric["unit"],
                "order": order,
                "series": [
                    {
                        **series,
                        "value_field": f"{series['series_id']}.{metric_id}",
                    }
                    for series in _SERIES
                ],
            }
        )
    return {
        "type": "line_chart",
        "layout": "small_multiples",
        "x_axis": {
            "field": "date",
            "type": "date",
            "sort": "ascending",
            "date_basis": "source_date",
        },
        "comparison_alignment": "day_index",
        "comparison_date_field": "comparison_date",
        "missing_dates": "zero",
        "charts": charts,
    }


def _daily_series(
    raw_series: Sequence[Any],
    *,
    period_start: date,
    comparison_start: date,
    day_count: int,
) -> list[dict[str, Any]]:
    raw_by_index: dict[int, Any] = {}
    for point in raw_series:
        day_index = _day_index(_field(point, "day_index"))
        if day_index in raw_by_index:
            raise ValueError(f"Duplicate traffic summary day_index: {day_index}")
        raw_by_index[day_index] = point

    expected_indexes = list(range(day_count))
    if sorted(raw_by_index) != expected_indexes:
        raise ValueError(
            "Traffic summary daily series must contain one point for every day"
        )

    normalized = []
    for day_index in expected_indexes:
        point = raw_by_index[day_index]
        expected_date = period_start + timedelta(days=day_index)
        expected_comparison_date = comparison_start + timedelta(days=day_index)
        actual_date = _iso_date(_field(point, "date"))
        actual_comparison_date = _iso_date(_field(point, "comparison_date"))
        if actual_date != expected_date.isoformat():
            raise ValueError(
                f"Traffic summary date is not aligned at day_index {day_index}"
            )
        if actual_comparison_date != expected_comparison_date.isoformat():
            raise ValueError(
                "Traffic summary comparison_date is not aligned at "
                f"day_index {day_index}"
            )
        normalized.append(
            {
                "day_index": day_index,
                "date": actual_date,
                "comparison_date": actual_comparison_date,
                "current": _metric_values(_field(point, "current")),
                "previous": _metric_values(_field(point, "previous")),
            }
        )
    return normalized


def _build_traffic_summary_report(
    *,
    row: Any,
    tenant: Mapping[str, Any],
) -> dict[str, Any]:
    period_start = date.fromisoformat(_iso_date(_field(row, "start_date")))
    period_end = date.fromisoformat(_iso_date(_field(row, "end_date")))
    comparison_start = date.fromisoformat(_iso_date(_field(row, "previous_start_date")))
    comparison_end = date.fromisoformat(_iso_date(_field(row, "previous_end_date")))
    day_count = (period_end - period_start).days + 1
    comparison_day_count = (comparison_end - comparison_start).days + 1
    if day_count <= 0 or comparison_day_count != day_count:
        raise ValueError("Traffic summary periods must have equal positive lengths")
    if comparison_end != period_start - timedelta(days=1):
        raise ValueError(
            "Traffic summary comparison period must immediately precede the period"
        )
    if comparison_start != period_start - timedelta(days=day_count):
        raise ValueError(
            "Traffic summary comparison start must preserve equal-length alignment"
        )

    current_period = _metric_values(_field(row, "current_period"))
    previous_period = _metric_values(_field(row, "previous_period"))
    raw_change_pct = _field(row, "change_pct")
    change_pct = {
        metric["metric_id"]: _percentage(_field(raw_change_pct, metric["metric_id"]))
        for metric in TRAFFIC_METRICS
    }
    headline_metrics = [
        {
            **metric,
            "current_value": current_period[metric["metric_id"]],
            "previous_value": previous_period[metric["metric_id"]],
            "change_pct": change_pct[metric["metric_id"]],
        }
        for metric in TRAFFIC_METRICS
    ]

    daily_series = _daily_series(
        list(_field(row, "daily_series")),
        period_start=period_start,
        comparison_start=comparison_start,
        day_count=day_count,
    )

    tenant_result = {
        "tenant_id": tenant["tenant_id"],
        "tenant_name": tenant["tenant_name"],
    }
    for field_name in ("requested_name", "resolved_name", "match_type"):
        if field_name in tenant:
            tenant_result[field_name] = tenant[field_name]

    return {
        "status": "ok",
        "report_type": REPORT_TYPE,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "date_basis": _date_basis(),
        "tenant": tenant_result,
        "data_source": {
            "project_id": tenant["project_id"],
            "dataset_id": tenant["dataset_id"],
        },
        "period": {
            "start_date": period_start.isoformat(),
            "end_date": period_end.isoformat(),
            "day_count": day_count,
        },
        "comparison_period": {
            "start_date": comparison_start.isoformat(),
            "end_date": comparison_end.isoformat(),
            "day_count": comparison_day_count,
            "strategy": "immediately_preceding_equal_length",
            "alignment": "day_index",
        },
        "headline_metrics": headline_metrics,
        "daily_series": daily_series,
        "presentation": _presentation(),
        # Compatibility aliases retained for clients of the pre-versioned shape.
        "current_period": current_period,
        "previous_period": previous_period,
        "change_pct": change_pct,
    }


def build_traffic_summary_report(
    *,
    row: Any,
    tenant: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the stable JSON contract without exposing malformed row details."""

    try:
        return _build_traffic_summary_report(
            row=row,
            tenant=tenant,
        )
    except TrafficSummaryReportError:
        raise
    except Exception as error:
        raise TrafficSummaryReportError() from error
