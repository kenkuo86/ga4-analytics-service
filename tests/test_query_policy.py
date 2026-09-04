from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from google.api_core.exceptions import Forbidden

import main
import mcp_server
from query_policy import PreparedQuery, QueryPolicy, QueryPolicyError


def _query(name: str = "metric") -> PreparedQuery:
    return PreparedQuery(
        name=name,
        sql="SELECT 1",
        query_parameters=[],
        labels={"component": "test"},
    )


class QueryPolicyTests(unittest.TestCase):
    def test_defaults_match_phase_four_decisions(self):
        policy = QueryPolicy.from_environment({})

        self.assertEqual(policy.max_date_range_days, 90)
        self.assertEqual(policy.earliest_date, date(2020, 10, 14))
        self.assertEqual(policy.max_bytes_per_job, 2_000_000_000)
        self.assertEqual(policy.max_bytes_per_request, 10_000_000_000)
        self.assertEqual(policy.job_timeout_ms, 60_000)
        self.assertEqual(policy.time_zone, "Asia/Taipei")

    def test_environment_overrides_are_validated(self):
        policy = QueryPolicy.from_environment(
            {
                "GA4_QUERY_MAX_DAYS": "31",
                "GA4_QUERY_EARLIEST_DATE": "2024-01-01",
                "GA4_QUERY_MAX_BYTES_PER_JOB": "100",
                "GA4_QUERY_MAX_BYTES_PER_REQUEST": "200",
                "GA4_QUERY_JOB_TIMEOUT_MS": "5000",
                "GA4_QUERY_TIME_ZONE": "UTC",
            }
        )

        self.assertEqual(policy.max_date_range_days, 31)
        self.assertEqual(policy.earliest_date, date(2024, 1, 1))
        self.assertEqual(policy.max_bytes_per_job, 100)
        self.assertEqual(policy.max_bytes_per_request, 200)
        self.assertEqual(policy.job_timeout_ms, 5000)
        self.assertEqual(policy.time_zone, "UTC")

        with self.assertRaises(ValueError):
            QueryPolicy.from_environment(
                {
                    "GA4_QUERY_MAX_BYTES_PER_JOB": "200",
                    "GA4_QUERY_MAX_BYTES_PER_REQUEST": "100",
                }
            )

    def test_date_validation_accepts_exactly_ninety_days(self):
        policy = QueryPolicy()

        parsed = policy.validate_date_range(
            "2026-06-01",
            "2026-08-29",
            today=date(2026, 9, 4),
        )

        self.assertEqual(parsed, (date(2026, 6, 1), date(2026, 8, 29)))

    def test_date_validation_rejects_every_invalid_boundary(self):
        policy = QueryPolicy()
        scenarios = [
            ("20260801", "2026-08-02", "invalid_date_format"),
            ("2026-02-30", "2026-03-01", "invalid_date_format"),
            ("2026-08-03", "2026-08-02", "invalid_date_range"),
            ("2020-10-13", "2020-10-14", "date_before_available_range"),
            ("2026-09-01", "2026-09-05", "future_date_not_allowed"),
            ("2026-06-01", "2026-08-30", "date_range_too_large"),
        ]

        for start_date, end_date, expected_code in scenarios:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(QueryPolicyError) as raised:
                    policy.validate_date_range(
                        start_date,
                        end_date,
                        today=date(2026, 9, 4),
                    )
                self.assertEqual(raised.exception.code, expected_code)

    def test_comparison_period_cannot_cross_earliest_date(self):
        policy = QueryPolicy()

        with self.assertRaises(QueryPolicyError) as raised:
            policy.validate_date_range(
                "2020-10-14",
                "2020-10-14",
                today=date(2026, 9, 4),
                comparison_periods=1,
            )

        self.assertEqual(raised.exception.code, "date_before_available_range")
        self.assertEqual(
            raised.exception.details["earliest_scanned_date"],
            "2020-10-13",
        )

    def test_preflight_rejects_single_job_before_execution(self):
        client = Mock()
        client.query.return_value = SimpleNamespace(total_bytes_processed=101)
        policy = QueryPolicy(max_bytes_per_job=100, max_bytes_per_request=200)

        with self.assertRaises(QueryPolicyError) as raised:
            policy.preflight_request(client, [_query()])

        self.assertEqual(raised.exception.code, "query_cost_limit_exceeded")
        self.assertEqual(raised.exception.details["estimated_bytes"], 101)
        config = client.query.call_args.kwargs["job_config"]
        self.assertTrue(config.dry_run)
        self.assertFalse(config.use_query_cache)

    def test_preflight_rejects_request_total(self):
        client = Mock()
        client.query.side_effect = [
            SimpleNamespace(total_bytes_processed=8),
            SimpleNamespace(total_bytes_processed=8),
        ]
        policy = QueryPolicy(max_bytes_per_job=10, max_bytes_per_request=15)

        with self.assertRaises(QueryPolicyError) as raised:
            policy.preflight_request(client, [_query("one"), _query("two")])

        self.assertEqual(raised.exception.code, "query_cost_limit_exceeded")
        self.assertEqual(raised.exception.details["estimated_request_bytes"], 16)

    def test_execute_sets_shared_job_controls(self):
        query_job = Mock()
        query_job.result.return_value = [SimpleNamespace(value=1)]
        client = Mock()
        client.query.return_value = query_job
        policy = QueryPolicy(
            max_bytes_per_job=123,
            max_bytes_per_request=456,
            job_timeout_ms=5000,
        )

        returned_job, rows = policy.execute(client, _query())

        self.assertIs(returned_job, query_job)
        self.assertEqual(len(rows), 1)
        config = client.query.call_args.kwargs["job_config"]
        self.assertEqual(config.maximum_bytes_billed, 123)
        self.assertTrue(config.use_query_cache)
        self.assertEqual(config.job_timeout_ms, "5000")
        self.assertEqual(config.labels["cost-policy"], "v1")
        self.assertEqual(config.labels["stage"], "execute")
        query_job.result.assert_called_once_with(timeout=5.0)

    def test_daily_quota_error_is_not_hidden_as_data_unavailable(self):
        error = Forbidden(
            "Custom quota exceeded: Your usage exceeded the custom quota for "
            "QueryUsagePerDay"
        )

        mapped = QueryPolicy().map_bigquery_error(error)

        self.assertIsNotNone(mapped)
        self.assertEqual(mapped.code, "daily_query_quota_exceeded")

    def test_rest_endpoint_returns_structured_policy_error(self):
        error = QueryPolicyError("date_range_too_large", "too long")

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
                        "start_date": "2026-01-01",
                        "end_date": "2026-04-01",
                    },
                )
        finally:
            main.app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["status"], "date_range_too_large")

    def test_rest_endpoint_uses_retryable_status_for_backend_policy_errors(self):
        main.app.dependency_overrides[main.require_rest_oauth] = lambda: {}
        try:
            with TestClient(main.app) as client:
                for code, expected_status in (
                    ("query_cost_estimate_failed", 503),
                    ("query_timeout", 504),
                ):
                    with (
                        self.subTest(code=code),
                        patch(
                            "main.get_traffic_summary",
                            side_effect=QueryPolicyError(code, "temporary failure"),
                        ),
                    ):
                        response = client.get(
                            "/traffic-summary",
                            params={
                                "customer_name": "customer",
                                "start_date": "2026-08-01",
                                "end_date": "2026-08-02",
                            },
                        )
                        self.assertEqual(response.status_code, expected_status)
        finally:
            main.app.dependency_overrides.clear()

    def test_mcp_tools_return_structured_policy_errors(self):
        cost_error = QueryPolicyError("query_cost_limit_exceeded", "too costly")
        quota_error = QueryPolicyError("daily_query_quota_exceeded", "quota used")

        with patch("mcp_server.query_ga4_semantic_metrics", side_effect=cost_error):
            semantic_result = mcp_server.query_ga4(
                "customer",
                ["total_sessions"],
                "2026-08-01",
                "2026-08-02",
            )
        with patch("mcp_server.get_traffic_summary", side_effect=quota_error):
            traffic_result = mcp_server.traffic_summary(
                "customer",
                "2026-08-01",
                "2026-08-02",
            )

        self.assertEqual(semantic_result["status"], "query_cost_limit_exceeded")
        self.assertEqual(traffic_result["status"], "daily_query_quota_exceeded")

    def test_registry_daily_quota_error_reaches_rest_and_mcp(self):
        registry_job = Mock()
        registry_job.result.side_effect = Forbidden(
            "Custom quota exceeded: Your usage exceeded the custom quota for "
            "QueryUsagePerDay"
        )
        client = Mock()
        client.query.return_value = registry_job

        main.app.dependency_overrides[main.require_rest_oauth] = lambda: {}
        try:
            with (
                patch("main.get_bigquery_client", return_value=client),
                TestClient(main.app) as rest_client,
            ):
                rest_response = rest_client.get(
                    "/traffic-summary",
                    params={
                        "customer_name": "customer",
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-02",
                    },
                )
                mcp_result = mcp_server.query_ga4(
                    "customer",
                    ["total_sessions"],
                    "2026-08-01",
                    "2026-08-02",
                )
        finally:
            main.app.dependency_overrides.clear()

        self.assertEqual(rest_response.status_code, 429)
        self.assertEqual(
            rest_response.json()["detail"]["status"],
            "daily_query_quota_exceeded",
        )
        self.assertEqual(mcp_result["status"], "daily_query_quota_exceeded")


if __name__ == "__main__":
    unittest.main()
