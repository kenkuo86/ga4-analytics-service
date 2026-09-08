from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
import mcp_server
from traffic_summary_report import (
    REPORT_SCHEMA_VERSION,
    REPORT_TYPE,
    TRAFFIC_METRICS,
    TrafficSummaryReportError,
    build_traffic_summary_report,
)


METRIC_IDS = [metric["metric_id"] for metric in TRAFFIC_METRICS]


def _values(
    total_sessions: int = 0,
    total_users: int = 0,
    new_users: int = 0,
    returning_users: int = 0,
) -> dict[str, int]:
    return {
        "total_sessions": total_sessions,
        "total_users": total_users,
        "new_users": new_users,
        "returning_users": returning_users,
    }


def _row(
    day_count: int,
    *,
    current_period: dict[str, int] | None = None,
    previous_period: dict[str, int] | None = None,
    change_pct: dict[str, float | None] | None = None,
    current_daily: list[dict[str, int]] | None = None,
    previous_daily: list[dict[str, int]] | None = None,
    reverse_daily: bool = False,
) -> SimpleNamespace:
    period_start = date(2026, 6, 1)
    comparison_start = period_start - timedelta(days=day_count)
    current_daily = current_daily or [_values() for _ in range(day_count)]
    previous_daily = previous_daily or [_values() for _ in range(day_count)]
    points = [
        SimpleNamespace(
            day_index=index,
            date=period_start + timedelta(days=index),
            comparison_date=comparison_start + timedelta(days=index),
            current=current_daily[index],
            previous=previous_daily[index],
        )
        for index in range(day_count)
    ]
    if reverse_daily:
        points.reverse()
    return SimpleNamespace(
        start_date=period_start,
        end_date=period_start + timedelta(days=day_count - 1),
        previous_start_date=comparison_start,
        previous_end_date=period_start - timedelta(days=1),
        current_period=current_period or _values(),
        previous_period=previous_period or _values(),
        change_pct=change_pct
        or {
            "total_sessions": None,
            "total_users": None,
            "new_users": None,
            "returning_users": None,
        },
        daily_series=points,
    )


def _report(row: SimpleNamespace) -> dict:
    return build_traffic_summary_report(
        row=row,
        tenant={
            "tenant_id": "5",
            "tenant_name": "維肯媒體部落格",
            "project_id": "customer-project",
            "dataset_id": "ga4_mar",
        },
    )


class TrafficSummaryReportTests(unittest.TestCase):
    def test_contract_has_fixed_version_metric_and_chart_order(self):
        report = _report(_row(1))

        self.assertEqual(report["report_type"], REPORT_TYPE)
        self.assertEqual(report["report_schema_version"], REPORT_SCHEMA_VERSION)
        self.assertEqual(
            [metric["metric_id"] for metric in report["headline_metrics"]],
            METRIC_IDS,
        )
        presentation = report["presentation"]
        self.assertEqual(presentation["type"], "line_chart")
        self.assertEqual(presentation["layout"], "small_multiples")
        self.assertEqual(
            [chart["chart_id"] for chart in presentation["charts"]],
            METRIC_IDS,
        )
        for chart in presentation["charts"]:
            self.assertEqual(
                [series["series_id"] for series in chart["series"]],
                ["current", "previous"],
            )
        self.assertEqual(presentation["x_axis"]["field"], "date")
        self.assertEqual(presentation["x_axis"]["date_basis"], "source_date")
        self.assertNotIn("time_zone", presentation["x_axis"])
        self.assertEqual(presentation["comparison_alignment"], "day_index")
        self.assertEqual(presentation["missing_dates"], "zero")
        self.assertEqual(
            report["date_basis"],
            {
                "type": "source_date",
                "source_field": "mar_ga_sessions.session_date",
                "source_data_type": "DATE",
                "time_zone_conversion": "none",
            },
        )
        self.assertNotIn("time_zone", report["period"])

    def test_one_multi_and_ninety_day_series_are_complete_and_aligned(self):
        for day_count in (1, 7, 90):
            with self.subTest(day_count=day_count):
                report = _report(_row(day_count, reverse_daily=True))

                self.assertEqual(report["period"]["day_count"], day_count)
                self.assertEqual(report["comparison_period"]["day_count"], day_count)
                self.assertEqual(len(report["daily_series"]), day_count)
                self.assertEqual(
                    [point["day_index"] for point in report["daily_series"]],
                    list(range(day_count)),
                )
                for point in report["daily_series"]:
                    current_date = date.fromisoformat(point["date"])
                    comparison_date = date.fromisoformat(point["comparison_date"])
                    self.assertEqual(
                        (current_date - comparison_date).days,
                        day_count,
                    )

    def test_missing_dates_remain_explicit_zero_points(self):
        report = _report(
            _row(
                3,
                current_daily=[
                    _values(3, 2, 1, 1),
                    _values(),
                    _values(4, 3, 2, 1),
                ],
                previous_daily=[_values(), _values(), _values()],
            )
        )

        self.assertEqual(report["daily_series"][1]["current"], _values())
        self.assertEqual(report["daily_series"][1]["previous"], _values())

    def test_headline_distinct_users_are_not_recomputed_from_daily_values(self):
        report = _report(
            _row(
                2,
                current_period=_values(2, 1, 1, 0),
                previous_period=_values(0, 0, 0, 0),
                current_daily=[
                    _values(1, 1, 1, 0),
                    _values(1, 1, 1, 0),
                ],
                change_pct={
                    "total_sessions": None,
                    "total_users": None,
                    "new_users": None,
                    "returning_users": None,
                },
            )
        )

        self.assertEqual(report["current_period"]["total_sessions"], 2)
        self.assertEqual(
            sum(point["current"]["total_sessions"] for point in report["daily_series"]),
            2,
        )
        self.assertEqual(report["current_period"]["total_users"], 1)
        self.assertEqual(
            sum(point["current"]["total_users"] for point in report["daily_series"]),
            2,
        )
        self.assertIsNone(report["change_pct"]["total_users"])

    def test_headlines_and_compatibility_aliases_are_identical(self):
        report = _report(
            _row(
                1,
                current_period=_values(100, 80, 60, 30),
                previous_period=_values(120, 90, 70, 35),
                change_pct={
                    "total_sessions": -16.67,
                    "total_users": -11.11,
                    "new_users": -14.29,
                    "returning_users": -14.29,
                },
            )
        )

        for headline in report["headline_metrics"]:
            metric_id = headline["metric_id"]
            self.assertEqual(
                headline["current_value"], report["current_period"][metric_id]
            )
            self.assertEqual(
                headline["previous_value"], report["previous_period"][metric_id]
            )
            self.assertEqual(headline["change_pct"], report["change_pct"][metric_id])

    def test_contract_is_json_serializable(self):
        report = _report(_row(90))

        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)

        self.assertIn('"report_schema_version": "1.0.0"', encoded)
        self.assertNotIn("datetime.date", encoded)

    def test_builder_rejects_incomplete_or_misaligned_series(self):
        incomplete = _row(2)
        incomplete.daily_series.pop()
        with self.assertRaises(TrafficSummaryReportError) as incomplete_error:
            _report(incomplete)
        self.assertEqual(incomplete_error.exception.code, "invalid_report_contract")
        self.assertNotIn("day", incomplete_error.exception.as_result()["message"])

        misaligned = _row(1)
        misaligned.daily_series[0].comparison_date -= timedelta(days=1)
        with self.assertRaises(TrafficSummaryReportError) as misaligned_error:
            _report(misaligned)
        self.assertEqual(misaligned_error.exception.code, "invalid_report_contract")

    def test_builder_rejects_comparison_period_gap_and_overlap(self):
        gap = _row(2)
        gap.previous_start_date -= timedelta(days=1)
        gap.previous_end_date -= timedelta(days=1)
        with self.assertRaises(TrafficSummaryReportError):
            _report(gap)

        overlap = _row(2)
        overlap.previous_start_date += timedelta(days=1)
        overlap.previous_end_date += timedelta(days=1)
        with self.assertRaises(TrafficSummaryReportError):
            _report(overlap)

    def test_counts_fail_closed_on_null_negative_or_non_integral_values(self):
        for invalid_value in (None, -1, 1.5, Decimal("1.5"), True, "1"):
            with self.subTest(invalid_value=invalid_value):
                row = _row(1)
                row.current_period["total_users"] = invalid_value
                with self.assertRaises(TrafficSummaryReportError) as raised:
                    _report(row)
                self.assertEqual(raised.exception.code, "invalid_report_contract")

        daily_row = _row(1)
        daily_row.daily_series[0].previous["new_users"] = None
        with self.assertRaises(TrafficSummaryReportError):
            _report(daily_row)

    def test_day_index_fails_closed_on_malformed_values(self):
        for invalid_value in (
            None,
            -1,
            0.0,
            1.5,
            Decimal("1.5"),
            True,
            "0",
        ):
            with self.subTest(invalid_value=invalid_value):
                row = _row(1)
                row.daily_series[0].day_index = invalid_value

                with self.assertRaises(TrafficSummaryReportError) as raised:
                    _report(row)

                self.assertEqual(raised.exception.code, "invalid_report_contract")

    def test_integral_decimal_count_is_accepted(self):
        row = _row(1)
        row.current_period["total_users"] = Decimal("2")
        row.daily_series[0].current["total_users"] = Decimal("2")
        row.daily_series[0].day_index = Decimal("0")

        report = _report(row)

        self.assertEqual(report["current_period"]["total_users"], 2)
        self.assertEqual(report["daily_series"][0]["current"]["total_users"], 2)

    def test_sql_uses_one_table_reference_and_preserves_distinct_rollups(self):
        sql = (Path(__file__).parents[1] / "queries" / "traffic_summary.sql").read_text(
            encoding="utf-8"
        )

        self.assertEqual(sql.count(".mar_ga_sessions`"), 1)
        self.assertEqual(sql.count("FROM aggregates"), 1)
        self.assertEqual(sql.count("CROSS JOIN pivoted"), 1)
        self.assertIn("GENERATE_ARRAY", sql)
        self.assertIn("LEFT JOIN", sql)
        self.assertIn("GROUP BY GROUPING SETS", sql)
        self.assertIn("GROUPING(d.day_index) AS is_headline", sql)
        self.assertIn("COUNT(DISTINCT s.user_pseudo_id)", sql)
        self.assertIn("WHERE s.session_date BETWEEN", sql)
        self.assertIn("SAFE_DIVIDE", sql)
        self.assertIn("ORDER BY day_index", sql)
        self.assertIn("AS daily_series", sql)
        self.assertIn("AS `current`", sql)
        self.assertIn("AS `previous`", sql)
        self.assertIn("AS current_metric_date", sql)
        self.assertIn("AS previous_metric_date", sql)
        self.assertNotIn(" AS current_date", sql)
        self.assertNotIn(" AS previous_date", sql)

    def test_rest_and_mcp_pass_through_the_same_contract(self):
        report = _report(_row(1))
        main.app.dependency_overrides[main.require_rest_oauth] = lambda: {}
        try:
            with (
                patch("main.get_traffic_summary", return_value=report) as rest_summary,
                TestClient(main.app) as client,
            ):
                response = client.get(
                    "/traffic-summary",
                    params={
                        "customer_name": "customer",
                        "start_date": "2026-06-01",
                        "end_date": "2026-06-01",
                        "include_query": "true",
                    },
                )
            with patch(
                "mcp_server.get_traffic_summary", return_value=report
            ) as mcp_summary:
                mcp_result = mcp_server.traffic_summary(
                    "customer",
                    "2026-06-01",
                    "2026-06-01",
                    include_query=True,
                )
        finally:
            main.app.dependency_overrides.clear()

        self.assertEqual(response.json(), report)
        self.assertEqual(mcp_result, report)
        rest_summary.assert_called_once_with(
            customer_name="customer",
            start_date="2026-06-01",
            end_date="2026-06-01",
            include_query=True,
        )
        mcp_summary.assert_called_once_with(
            customer_name="customer",
            start_date="2026-06-01",
            end_date="2026-06-01",
            include_query=True,
        )

    def test_rest_and_mcp_return_the_same_safe_contract_error(self):
        error = TrafficSummaryReportError()
        main.app.dependency_overrides[main.require_rest_oauth] = lambda: {}
        try:
            with (
                patch("main.get_traffic_summary", side_effect=error),
                TestClient(main.app) as client,
            ):
                response = client.get(
                    "/traffic-summary",
                    params={
                        "customer_name": "customer",
                        "start_date": "2026-06-01",
                        "end_date": "2026-06-01",
                    },
                )
            with patch("mcp_server.get_traffic_summary", side_effect=error):
                mcp_result = mcp_server.traffic_summary(
                    "customer",
                    "2026-06-01",
                    "2026-06-01",
                )
        finally:
            main.app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], error.as_result())
        self.assertEqual(mcp_result, error.as_result())
        self.assertEqual(
            error.as_result(),
            {
                "status": "invalid_report_contract",
                "message": "目前無法產生流量摘要報表，請稍後再試。",
            },
        )


if __name__ == "__main__":
    unittest.main()
