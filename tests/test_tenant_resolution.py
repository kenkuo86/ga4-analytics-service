from __future__ import annotations

from datetime import date
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from main import (
    TenantResolutionError,
    get_available_customers,
    get_bigquery_client,
    get_customer_status,
    get_tenant_config,
    get_traffic_summary,
)
from query_policy import QueryPolicyError


def _row(**overrides):
    values = {
        "tenant_id": "5",
        "tenant_name": "維肯媒體部落格",
        "project_id": "my-ga4-project",
        "status": "active",
        "ec": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client_with_rows(rows):
    query_job = Mock()
    query_job.result.return_value = rows
    client = Mock()
    client.query.return_value = query_job
    return client


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

    def test_customer_status_does_not_require_analytics_access(self):
        client = _client_with_rows([_row(project_id="other-project")])

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
        execution_config = client.query.call_args_list[2].kwargs["job_config"]
        self.assertEqual(execution_config.maximum_bytes_billed, 2_000_000_000)
        self.assertTrue(execution_config.use_query_cache)
        self.assertEqual(execution_config.job_timeout_ms, "60000")
        self.assertEqual(execution_config.labels["component"], "traffic-summary")

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


if __name__ == "__main__":
    unittest.main()
