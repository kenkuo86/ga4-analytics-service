from __future__ import annotations

from datetime import date, timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

import main
import mcp_server
from main import (
    TenantResolutionError,
    get_available_customers,
    get_bigquery_client,
    get_customer_status,
    get_tenant_config,
    get_traffic_summary,
    query_ga4_semantic_metrics,
)
from query_policy import QueryPolicyError
from traffic_summary_report import TrafficSummaryReportError


def _row(**overrides):
    values = {
        "tenant_id": "5",
        "tenant_name": "維肯媒體部落格",
        "project_id": "my-ga4-project",
        "status": "active",
        "ec": False,
        "aliases": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client_with_rows(rows):
    query_job = Mock()
    query_job.result.return_value = rows
    client = Mock()
    client.query.return_value = query_job
    return client


def _traffic_daily_series(start_date, comparison_start_date, day_count):
    empty_values = {
        "total_sessions": 0,
        "total_users": 0,
        "new_users": 0,
        "returning_users": 0,
    }
    return [
        SimpleNamespace(
            day_index=day_index,
            date=start_date + timedelta(days=day_index),
            comparison_date=comparison_start_date + timedelta(days=day_index),
            current=empty_values,
            previous=empty_values,
        )
        for day_index in range(day_count)
    ]


class TenantResolutionTests(unittest.TestCase):
    def test_bigquery_billing_project_override_wins_over_adc_project(self):
        credentials = Mock()
        with (
            unittest.mock.patch(
                "main.google.auth.default",
                return_value=(credentials, "detected-project"),
            ),
            unittest.mock.patch("main.bigquery.Client") as client_class,
            unittest.mock.patch.dict(
                os.environ,
                {"BIGQUERY_BILLING_PROJECT": "ga4-reports-dev"},
            ),
        ):
            get_bigquery_client()

        client_class.assert_called_once_with(
            credentials=credentials,
            project="ga4-reports-dev",
        )

    def test_blank_billing_project_falls_back_to_adc_project(self):
        credentials = Mock()
        with (
            unittest.mock.patch(
                "main.google.auth.default",
                return_value=(credentials, "detected-project"),
            ),
            unittest.mock.patch("main.bigquery.Client") as client_class,
            unittest.mock.patch.dict(
                os.environ,
                {"BIGQUERY_BILLING_PROJECT": "   "},
            ),
        ):
            get_bigquery_client()

        client_class.assert_called_once_with(
            credentials=credentials,
            project="detected-project",
        )

    def test_available_customers_returns_names_only(self):
        client = _client_with_rows(
            [
                SimpleNamespace(tenant_name="初衣食午股份有限公司"),
                SimpleNamespace(tenant_name="維肯媒體部落格"),
            ]
        )

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = get_available_customers()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            result["customers"],
            ["初衣食午股份有限公司", "維肯媒體部落格"],
        )
        self.assertNotIn("project_id", result)
        sql = client.query.call_args.args[0]
        self.assertIn("LOWER(TRIM(status)) = 'active'", sql)
        self.assertIn("NULLIF(TRIM(project_id), '') IS NOT NULL", sql)
        self.assertIn("HAVING COUNT(*) = 1", sql)

    def test_registry_query_supports_aliases_and_partial_candidates(self):
        client = _client_with_rows(
            [
                _row(
                    tenant_name="東方美企業",
                    aliases="小太陽|Sunny Digital",
                    match_type="alias",
                )
            ]
        )

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = get_customer_status("Ｓｕｎｎｙ Digital")

        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["requested_name"], "Ｓｕｎｎｙ Digital")
        self.assertEqual(result["match_type"], "alias")
        sql = client.query.call_args.args[0]
        self.assertIn("aliases", sql)
        self.assertIn("UNNEST(SPLIT", sql)
        self.assertIn("STRPOS(normalized_name, normalized_request)", sql)

    def test_active_customer_resolves_to_ga4_mar(self):
        client = _client_with_rows([_row()])

        tenant = get_tenant_config(client, "  維肯媒體部落格  ")

        self.assertEqual(tenant["tenant_id"], "5")
        self.assertEqual(tenant["project_id"], "my-ga4-project")
        self.assertEqual(tenant["dataset_id"], "ga4_mar")
        self.assertEqual(tenant["semantic_profile"], "non_ecommerce")
        _, kwargs = client.query.call_args
        parameter = kwargs["job_config"].query_parameters[0]
        self.assertEqual(parameter.name, "customer_name")
        self.assertEqual(parameter.value, "維肯媒體部落格")

    def test_exact_alias_resolves_to_formal_tenant_config(self):
        client = _client_with_rows(
            [
                _row(
                    tenant_name="東方美企業",
                    aliases="東方美|Orient Beauty",
                    match_type="alias",
                )
            ]
        )

        tenant = get_tenant_config(client, "Orient Beauty")

        self.assertEqual(tenant["tenant_name"], "東方美企業")
        self.assertEqual(tenant["requested_name"], "Orient Beauty")
        self.assertEqual(tenant["resolved_name"], "東方美企業")
        self.assertEqual(tenant["match_type"], "alias")

    def test_formal_name_takes_precedence_over_alias_match(self):
        client = _client_with_rows(
            [
                _row(
                    tenant_name="Orient Beauty",
                    match_type="exact",
                ),
                _row(
                    tenant_id="6",
                    tenant_name="另一家企業",
                    aliases="Orient Beauty",
                    match_type="alias",
                ),
            ]
        )

        tenant = get_tenant_config(client, "Orient Beauty")

        self.assertEqual(tenant["tenant_name"], "Orient Beauty")
        self.assertEqual(tenant["match_type"], "exact")

    def test_duplicate_alias_matches_fail_closed_at_runtime(self):
        client = _client_with_rows(
            [
                _row(
                    tenant_name="東方美企業",
                    aliases="Orient Beauty",
                    match_type="alias",
                ),
                _row(
                    tenant_id="6",
                    tenant_name="另一家企業",
                    aliases="Orient Beauty",
                    match_type="alias",
                ),
            ]
        )

        with self.assertRaises(TenantResolutionError) as raised:
            get_tenant_config(client, "Orient Beauty")

        self.assertEqual(raised.exception.code, "ambiguous_tenant")
        self.assertEqual(raised.exception.match_type, "alias")
        self.assertEqual(client.query.call_count, 1)

    def test_unique_partial_candidate_requires_confirmation_before_data_query(self):
        client = _client_with_rows(
            [_row(tenant_name="東方美企業", match_type="partial")]
        )

        with self.assertRaises(TenantResolutionError) as raised:
            get_tenant_config(client, "東方美")

        error = raised.exception
        self.assertEqual(error.code, "tenant_confirmation_required")
        result = error.as_result()
        self.assertEqual(result["requested_name"], "東方美")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "partial")
        self.assertEqual(result["candidates"][0]["resolved_name"], "東方美企業")
        self.assertEqual(client.query.call_count, 1)

    def test_multiple_partial_candidates_fail_closed_without_data_query(self):
        client = _client_with_rows(
            [
                _row(tenant_name="東方美企業", match_type="partial"),
                _row(
                    tenant_id="6",
                    tenant_name="東方美國際",
                    match_type="partial",
                ),
            ]
        )

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(TenantResolutionError) as raised,
        ):
            query_ga4_semantic_metrics(
                customer_name="東方美",
                metric_ids=["total_sessions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
            )

        self.assertEqual(raised.exception.code, "ambiguous_tenant")
        self.assertEqual(raised.exception.as_result()["match_type"], "partial")
        self.assertEqual(
            [candidate["resolved_name"] for candidate in raised.exception.candidates],
            ["東方美企業", "東方美國際"],
        )
        self.assertEqual(client.query.call_count, 1)

    def test_broad_partial_candidate_is_not_auto_resolved(self):
        client = _client_with_rows(
            [_row(tenant_name="東方美企業", match_type="partial")]
        )

        with self.assertRaises(TenantResolutionError) as raised:
            get_tenant_config(client, "美")

        self.assertEqual(raised.exception.code, "customer_name_too_broad")
        self.assertIsNone(raised.exception.resolved_name)

    def test_mcp_customer_lookup_returns_partial_candidates(self):
        client = _client_with_rows(
            [_row(tenant_name="東方美企業", match_type="partial")]
        )

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = mcp_server.customer_lookup("東方美")

        self.assertEqual(result["status"], "tenant_confirmation_required")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "partial")
        self.assertEqual(client.query.call_count, 1)

    def test_rest_partial_candidate_returns_conflict_without_data_query(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _row(tenant_name="東方美企業", match_type="partial")
        ]
        client = Mock()
        client.query.return_value = registry_job

        main.app.dependency_overrides[main.require_rest_oauth] = lambda: {}
        try:
            with (
                unittest.mock.patch("main.get_bigquery_client", return_value=client),
                TestClient(main.app) as test_client,
            ):
                response = test_client.get(
                    "/traffic-summary",
                    params={
                        "customer_name": "東方美",
                        "start_date": "2026-08-17",
                        "end_date": "2026-08-23",
                    },
                )
        finally:
            main.app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["status"],
            "tenant_confirmation_required",
        )
        self.assertEqual(client.query.call_count, 1)

    def test_ec_true_resolves_to_ecommerce_profile(self):
        tenant = get_tenant_config(
            _client_with_rows([_row(ec=True)]),
            "維肯媒體部落格",
        )

        self.assertEqual(tenant["semantic_profile"], "ecommerce")

    def test_blank_ec_retains_non_ecommerce_profile(self):
        tenant = get_tenant_config(
            _client_with_rows([_row(ec=None)]),
            "維肯媒體部落格",
        )

        self.assertEqual(tenant["semantic_profile"], "non_ecommerce")

    def test_unknown_customer_is_not_found(self):
        with self.assertRaises(TenantResolutionError) as raised:
            get_tenant_config(_client_with_rows([]), "不存在的客戶")

        self.assertEqual(raised.exception.code, "tenant_not_found")
        self.assertEqual(raised.exception.as_result()["customer_name"], "不存在的客戶")

    def test_non_active_customer_is_reported_separately(self):
        with self.assertRaises(TenantResolutionError) as raised:
            get_tenant_config(
                _client_with_rows([_row(status="provisioning")]),
                "維肯媒體部落格",
            )

        self.assertEqual(raised.exception.code, "tenant_inactive")

    def test_duplicate_customer_name_is_ambiguous(self):
        with self.assertRaises(TenantResolutionError) as raised:
            get_tenant_config(
                _client_with_rows([_row(), _row(tenant_id="6")]),
                "維肯媒體部落格",
            )

        self.assertEqual(raised.exception.code, "ambiguous_tenant")

    def test_blank_customer_name_is_rejected_without_query(self):
        client = Mock()

        with self.assertRaises(TenantResolutionError) as raised:
            get_tenant_config(client, "   ")

        self.assertEqual(raised.exception.code, "invalid_customer_name")
        client.query.assert_not_called()

    def test_invalid_project_id_returns_alias_context_to_rest_and_mcp(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _row(
                tenant_name="東方美企業",
                project_id="invalid.project",
                aliases="Orient Beauty",
                match_type="alias",
            )
        ]
        client = Mock()
        client.query.return_value = registry_job

        main.app.dependency_overrides[main.require_rest_oauth] = lambda: {}
        try:
            with (
                unittest.mock.patch("main.get_bigquery_client", return_value=client),
                TestClient(main.app) as test_client,
            ):
                mcp_result = mcp_server.query_ga4(
                    "Orient Beauty",
                    ["total_sessions"],
                    "2026-08-17",
                    "2026-08-23",
                )
                response = test_client.get(
                    "/traffic-summary",
                    params={
                        "customer_name": "Orient Beauty",
                        "start_date": "2026-08-17",
                        "end_date": "2026-08-23",
                    },
                )
        finally:
            main.app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 409)
        for result in (mcp_result, response.json()["detail"]):
            self.assertEqual(result["status"], "data_unavailable")
            self.assertEqual(result["requested_name"], "Orient Beauty")
            self.assertEqual(result["resolved_name"], "東方美企業")
            self.assertEqual(result["match_type"], "alias")
        self.assertEqual(client.query.call_count, 2)

    def test_customer_status_does_not_require_analytics_access(self):
        client = _client_with_rows(
            [_row(tenant_name="東方美企業", project_id="other-project")]
        )

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = get_customer_status("東方美企業")

        self.assertEqual(result["status"], "customer_found")
        self.assertTrue(result["analytics_available"])
        self.assertEqual(result["semantic_profile"], "non_ecommerce")
        self.assertEqual(
            result["data_source"],
            {
                "project_id": "other-project",
                "dataset_id": "ga4_mar",
            },
        )

    def test_customer_without_project_has_no_data_source(self):
        client = _client_with_rows([_row(project_id=None)])

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = get_customer_status("維肯媒體部落格")

        self.assertFalse(result["analytics_available"])
        self.assertIsNone(result["data_source"])

    def test_traffic_summary_returns_registry_routing_context(self):
        registry_job = Mock()
        registry_job.result.return_value = [_row(project_id="customer-project")]
        summary_job = Mock()
        summary_job.result.return_value = [
            SimpleNamespace(
                start_date=date(2026, 8, 17),
                end_date=date(2026, 8, 23),
                previous_start_date=date(2026, 8, 10),
                previous_end_date=date(2026, 8, 16),
                current_period={
                    "total_sessions": 100,
                    "total_users": 80,
                    "new_users": 60,
                    "returning_users": 30,
                },
                previous_period={
                    "total_sessions": 120,
                    "total_users": 90,
                    "new_users": 70,
                    "returning_users": 35,
                },
                change_pct={
                    "total_sessions": -16.67,
                    "total_users": -11.11,
                    "new_users": -14.29,
                    "returning_users": -14.29,
                },
                daily_series=_traffic_daily_series(
                    date(2026, 8, 17),
                    date(2026, 8, 10),
                    7,
                ),
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, summary_job]

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = get_traffic_summary(
                "維肯媒體部落格",
                "2026-08-17",
                "2026-08-23",
            )

        self.assertEqual(
            result["data_source"],
            {
                "project_id": "customer-project",
                "dataset_id": "ga4_mar",
            },
        )
        self.assertEqual(result["report_type"], "traffic_summary")
        self.assertEqual(result["report_schema_version"], "1.0.0")
        self.assertEqual(len(result["daily_series"]), 7)
        self.assertEqual(result["presentation"]["type"], "line_chart")
        self.assertNotIn("query_provenance", result)
        self.assertEqual(len(client.query.call_args_list), 3)
        execution_config = client.query.call_args_list[2].kwargs["job_config"]
        self.assertEqual(execution_config.maximum_bytes_billed, 2_000_000_000)
        self.assertTrue(execution_config.use_query_cache)
        self.assertEqual(execution_config.job_timeout_ms, "60000")
        self.assertEqual(execution_config.labels["component"], "traffic-summary")

    def test_traffic_summary_returns_query_provenance_when_requested(self):
        registry_job = Mock()
        registry_job.result.return_value = [_row(project_id="customer-project")]
        summary_job = Mock()
        summary_job.job_id = "job-traffic-summary"
        summary_job.cache_hit = True
        summary_job.total_bytes_processed = 8_000_000
        summary_job.total_bytes_billed = 0
        summary_job.result.return_value = [
            SimpleNamespace(
                start_date=date(2026, 8, 17),
                end_date=date(2026, 8, 23),
                previous_start_date=date(2026, 8, 10),
                previous_end_date=date(2026, 8, 16),
                current_period={
                    "total_sessions": 100,
                    "total_users": 80,
                    "new_users": 60,
                    "returning_users": 30,
                },
                previous_period={
                    "total_sessions": 120,
                    "total_users": 90,
                    "new_users": 70,
                    "returning_users": 35,
                },
                change_pct={
                    "total_sessions": -16.67,
                    "total_users": -11.11,
                    "new_users": -14.29,
                    "returning_users": -14.29,
                },
                daily_series=_traffic_daily_series(
                    date(2026, 8, 17),
                    date(2026, 8, 10),
                    7,
                ),
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=7_000_000)
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, summary_job]

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = get_traffic_summary(
                "維肯媒體部落格",
                "2026-08-17",
                "2026-08-23",
                include_query=True,
            )

        provenance = result["query_provenance"]
        self.assertEqual(provenance["schema_version"], "1.0.0")
        self.assertEqual(len(provenance["queries"]), 1)
        query = provenance["queries"][0]
        self.assertEqual(query["metric_id"], "traffic_summary")
        self.assertEqual(query["job_id"], "job-traffic-summary")
        self.assertTrue(query["cache_hit"])
        self.assertEqual(query["bytes_processed"], 8_000_000)
        self.assertEqual(query["bytes_billed"], 0)
        self.assertEqual(query["estimated_bytes_processed"], 7_000_000)
        self.assertEqual(
            query["parameters"],
            [
                {"name": "start_date", "type": "DATE", "value": "2026-08-17"},
                {"name": "end_date", "type": "DATE", "value": "2026-08-23"},
            ],
        )

    def test_failed_traffic_query_keeps_structured_provenance(self):
        registry_job = Mock()
        registry_job.result.return_value = [_row(project_id="customer-project")]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        failed_job = Mock()
        failed_job.job_id = "job-traffic-failed"
        failed_job.cache_hit = False
        failed_job.total_bytes_processed = 6_000_000
        failed_job.total_bytes_billed = 6_000_000
        failed_job.result.side_effect = RuntimeError("query failed")
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, failed_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(TenantResolutionError) as raised,
        ):
            get_traffic_summary(
                "維肯媒體部落格",
                "2026-08-17",
                "2026-08-23",
                include_query=True,
            )

        result = raised.exception.as_result()
        self.assertEqual(result["status"], "data_unavailable")
        self.assertEqual(
            result["details"]["query_provenance"]["queries"][0]["job_id"],
            "job-traffic-failed",
        )

    def test_failed_traffic_query_preserves_alias_resolution_context(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
                project_id="customer-project",
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        failed_job = Mock()
        failed_job.job_id = "job-alias-traffic-failed"
        failed_job.result.side_effect = RuntimeError("query failed")
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, failed_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(TenantResolutionError) as raised,
        ):
            get_traffic_summary(
                "Orient Beauty",
                "2026-08-17",
                "2026-08-23",
                include_query=True,
            )

        result = raised.exception.as_result()
        self.assertEqual(result["status"], "data_unavailable")
        self.assertEqual(result["requested_name"], "Orient Beauty")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "alias")
        self.assertEqual(
            result["details"]["query_provenance"]["queries"][0]["job_id"],
            "job-alias-traffic-failed",
        )

    def test_failed_traffic_row_iteration_is_marked_failed(self):
        class FailingRows:
            def __init__(self):
                self.returned_first_row = False

            def __iter__(self):
                return self

            def __next__(self):
                if not self.returned_first_row:
                    self.returned_first_row = True
                    return SimpleNamespace()
                raise RuntimeError("next page failed")

        registry_job = Mock()
        registry_job.result.return_value = [_row(project_id="customer-project")]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        failed_job = Mock()
        failed_job.job_id = "job-traffic-page-failed"
        failed_job.cache_hit = True
        failed_job.total_bytes_processed = 7_000_000
        failed_job.total_bytes_billed = 6_000_000
        failed_job.result.return_value = FailingRows()
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, failed_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(TrafficSummaryReportError) as raised,
        ):
            get_traffic_summary(
                "維肯媒體部落格",
                "2026-08-17",
                "2026-08-23",
                include_query=True,
            )

        query = raised.exception.as_result()["details"]["query_provenance"][
            "queries"
        ][0]
        self.assertEqual(query["status"], "failed")
        self.assertEqual(query["job_id"], "job-traffic-page-failed")
        self.assertTrue(query["cache_hit"])

    def test_traffic_summary_validates_dates_before_bigquery(self):
        with (
            unittest.mock.patch("main.get_bigquery_client") as get_client,
            self.assertRaises(QueryPolicyError) as raised,
        ):
            get_traffic_summary(
                "維肯媒體部落格",
                "2026-01-01",
                "2026-04-01",
            )

        self.assertEqual(raised.exception.code, "date_range_too_large")
        self.assertEqual(raised.exception.requested_name, "維肯媒體部落格")
        self.assertIsNone(raised.exception.resolved_name)
        self.assertEqual(raised.exception.match_type, "none")
        get_client.assert_not_called()

    def test_traffic_summary_comparison_stays_after_earliest_date(self):
        with (
            unittest.mock.patch("main.get_bigquery_client") as get_client,
            self.assertRaises(QueryPolicyError) as raised,
        ):
            get_traffic_summary(
                "維肯媒體部落格",
                "2020-10-14",
                "2020-10-14",
            )

        self.assertEqual(raised.exception.code, "date_before_available_range")
        self.assertEqual(raised.exception.requested_name, "維肯媒體部落格")
        self.assertIsNone(raised.exception.resolved_name)
        self.assertEqual(raised.exception.match_type, "none")
        get_client.assert_not_called()

    def test_traffic_summary_cost_limit_blocks_data_execution(self):
        registry_job = Mock()
        registry_job.result.return_value = [_row(project_id="customer-project")]
        dry_run_job = SimpleNamespace(total_bytes_processed=2_000_000_001)
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(QueryPolicyError) as raised,
        ):
            get_traffic_summary(
                "維肯媒體部落格",
                "2026-08-17",
                "2026-08-23",
            )

        self.assertEqual(raised.exception.code, "query_cost_limit_exceeded")
        self.assertEqual(client.query.call_count, 2)

    def test_traffic_summary_policy_preflight_preserves_alias_resolution_context(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
                project_id="customer-project",
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=2_000_000_001)
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(QueryPolicyError) as raised,
        ):
            get_traffic_summary(
                "Orient Beauty",
                "2026-08-17",
                "2026-08-23",
            )

        result = raised.exception.as_result()
        self.assertEqual(result["status"], "query_cost_limit_exceeded")
        self.assertEqual(result["requested_name"], "Orient Beauty")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "alias")

    def test_traffic_summary_policy_execution_preserves_alias_resolution_context(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
                project_id="customer-project",
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=1)
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job]
        policy_error = QueryPolicyError("query_timeout", "temporary failure")

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            unittest.mock.patch(
                "query_policy.QueryPolicy.execute",
                side_effect=policy_error,
            ),
            self.assertRaises(QueryPolicyError) as raised,
        ):
            get_traffic_summary(
                "Orient Beauty",
                "2026-08-17",
                "2026-08-23",
            )

        result = raised.exception.as_result()
        self.assertEqual(result["status"], "query_timeout")
        self.assertEqual(result["requested_name"], "Orient Beauty")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "alias")

    def test_traffic_summary_empty_result_is_a_safe_contract_error(self):
        registry_job = Mock()
        registry_job.result.return_value = [_row(project_id="customer-project")]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        summary_job = Mock()
        summary_job.result.return_value = []
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, summary_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(TrafficSummaryReportError) as raised,
        ):
            get_traffic_summary(
                "維肯媒體部落格",
                "2026-08-17",
                "2026-08-23",
            )

        self.assertEqual(raised.exception.code, "invalid_report_contract")
        self.assertEqual(
            raised.exception.as_result()["message"],
            "目前無法產生流量摘要報表，請稍後再試。",
        )

    def test_traffic_summary_report_errors_preserve_alias_resolution_context(self):
        class FailingRows:
            def __iter__(self):
                return self

            def __next__(self):
                raise RuntimeError("next page failed")

        cases = {
            "empty": [],
            "multiple": [SimpleNamespace(), SimpleNamespace()],
            "iterator": FailingRows(),
            "malformed": [SimpleNamespace()],
        }
        for failure_mode, rows in cases.items():
            with self.subTest(failure_mode=failure_mode):
                registry_job = Mock()
                registry_job.result.return_value = [
                    _row(
                        tenant_name="東方美企業",
                        aliases="Orient Beauty",
                        match_type="alias",
                        project_id="customer-project",
                    )
                ]
                dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
                summary_job = Mock()
                summary_job.result.return_value = rows
                client = Mock()
                client.query.side_effect = [registry_job, dry_run_job, summary_job]

                with (
                    unittest.mock.patch(
                        "main.get_bigquery_client",
                        return_value=client,
                    ),
                    self.assertRaises(TrafficSummaryReportError) as raised,
                ):
                    get_traffic_summary(
                        "Orient Beauty",
                        "2026-08-17",
                        "2026-08-23",
                    )

                result = raised.exception.as_result()
                self.assertEqual(result["status"], "invalid_report_contract")
                self.assertEqual(result["requested_name"], "Orient Beauty")
                self.assertEqual(result["resolved_name"], "東方美企業")
                self.assertEqual(result["match_type"], "alias")


if __name__ == "__main__":
    unittest.main()
