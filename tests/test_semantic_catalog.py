from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from main import query_ga4_semantic_metrics
from query_policy import QueryPolicy, QueryPolicyError
from semantic_catalog import SemanticCatalog, SemanticCatalogError, semantic_catalog


def _tenant_row(*, ec: bool | None = False):
    return SimpleNamespace(
        tenant_id="71",
        tenant_name="初衣食午股份有限公司",
        project_id="customer-project",
        status="active",
        ec=ec,
    )


class SemanticCatalogTests(unittest.TestCase):
    def test_compiled_catalog_has_expected_profile_counts(self):
        self.assertEqual(semantic_catalog.version, "1.1.0")
        self.assertEqual(
            semantic_catalog.profiles["non_ecommerce"]["metric_count"],
            80,
        )
        self.assertEqual(
            semantic_catalog.profiles["ecommerce"]["metric_count"],
            95,
        )
        total_users = semantic_catalog.profiles["non_ecommerce"]["metrics"][
            "total_users"
        ]
        self.assertEqual(total_users["status"], "published")
        self.assertEqual(total_users["model"], "mar_ga_sessions")
        self.assertEqual(
            total_users["type_params"]["agg_time_dimension"],
            "session_date",
        )
        self.assertIn("Canonical policy", total_users["definition_resolution"])

    def test_search_finds_source_medium_metrics(self):
        result = semantic_catalog.search(
            "來源媒介的工作階段與參與度",
            profile="non_ecommerce",
            limit=20,
        )

        metric_ids = {metric["metric_id"] for metric in result["metrics"]}
        self.assertIn("sessions_by_source_medium", metric_ids)
        self.assertIn("engagement_by_source_medium", metric_ids)
        source_medium = next(
            metric
            for metric in result["metrics"]
            if metric["metric_id"] == "sessions_by_source_medium"
        )
        self.assertEqual(source_medium["date_scope"], "requested_period")

    def test_shared_definition_resolves_without_profile(self):
        profile, resolution = semantic_catalog.resolve_profile(
            ["total_sessions"],
            None,
        )

        self.assertEqual(profile, "non_ecommerce")
        self.assertEqual(resolution, "shared_definition")

    def test_profile_specific_definition_requires_profile(self):
        with self.assertRaises(SemanticCatalogError) as raised:
            semantic_catalog.resolve_profile(["total_conversions"], None)

        self.assertEqual(raised.exception.code, "semantic_profile_required")

    def test_total_users_compiles_from_sessions_model(self):
        sql, _ = semantic_catalog.compile_sql(
            profile="non_ecommerce",
            metric_id="total_users",
            project_id="customer-project",
            dataset_id="ga4_mar",
            result_limit=20,
        )

        self.assertIn("mar_ga_sessions", sql)
        self.assertNotIn("mar_ga_events", sql)

    def test_compile_sql_resolves_only_approved_placeholders(self):
        sql, metric = semantic_catalog.compile_sql(
            profile="non_ecommerce",
            metric_id="sessions_by_source_medium",
            project_id="customer-project",
            dataset_id="ga4_mar",
            result_limit=20,
        )

        self.assertEqual(metric["dimensions"], ["session_source", "session_medium"])
        self.assertIn("`customer-project.ga4_mar.mar_ga_sessions`", sql)
        self.assertIn("@start_date", sql)
        self.assertIn("@end_date", sql)
        self.assertIn("LIMIT 21", sql)
        self.assertNotIn("ga4_bq_id", sql)

    def test_compile_sql_rejects_untrusted_project_identifier(self):
        with self.assertRaises(SemanticCatalogError) as raised:
            semantic_catalog.compile_sql(
                profile="non_ecommerce",
                metric_id="total_sessions",
                project_id="project`; DROP TABLE x; --",
                dataset_id="ga4_mar",
                result_limit=20,
            )

        self.assertEqual(raised.exception.code, "invalid_tenant_routing")

    def test_catalog_rejects_unapproved_table_reference(self):
        catalog = copy.deepcopy(semantic_catalog.catalog)
        catalog["profiles"]["ecommerce"]["metrics"]["total_revenue"][
            "sql_template"
        ] += "\nUNION ALL SELECT 1 FROM `other-project.secret.table`"

        with self.assertRaises(ValueError):
            SemanticCatalog(catalog)

    def test_catalog_rejects_multiple_or_mutating_statements(self):
        catalog = copy.deepcopy(semantic_catalog.catalog)
        catalog["profiles"]["ecommerce"]["metrics"]["total_revenue"][
            "sql_template"
        ] += "; DROP TABLE `ga4_bq_id.ga4_mar.mar_ga_sessions`"

        with self.assertRaises(ValueError):
            SemanticCatalog(catalog)

    def test_generic_query_resolves_tenant_and_serializes_rows(self):
        registry_job = Mock()
        registry_job.result.return_value = [_tenant_row()]
        metric_job = Mock()
        metric_job.result.return_value = [
            {
                "session_source": "google",
                "session_medium": "organic",
                "sessions_by_source_medium": 123,
            }
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, metric_job]

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = query_ga4_semantic_metrics(
                customer_name="初衣食午股份有限公司",
                metric_ids=["sessions_by_source_medium"],
                start_date="2026-08-17",
                end_date="2026-08-23",
                limit=20,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["semantic"]["catalog_version"], "1.1.0")
        self.assertEqual(result["semantic"]["profile"], "non_ecommerce")
        self.assertEqual(
            result["semantic"]["profile_resolution"],
            "tenant_registry.ec",
        )
        self.assertEqual(result["metrics"][0]["date_scope"], "requested_period")
        self.assertEqual(
            result["metrics"][0]["rows"][0],
            {
                "session_source": "google",
                "session_medium": "organic",
                "sessions_by_source_medium": 123,
            },
        )
        semantic_sql = client.query.call_args_list[2].args[0]
        self.assertIn("`customer-project.ga4_mar.mar_ga_sessions`", semantic_sql)
        execution_config = client.query.call_args_list[2].kwargs["job_config"]
        self.assertEqual(execution_config.maximum_bytes_billed, 2_000_000_000)
        self.assertTrue(execution_config.use_query_cache)

    def test_generic_query_uses_ecommerce_profile_from_registry(self):
        registry_job = Mock()
        registry_job.result.return_value = [_tenant_row(ec=True)]
        metric_job = Mock()
        metric_job.result.return_value = [{"total_conversions": 12}]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, metric_job]

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = query_ga4_semantic_metrics(
                customer_name="初衣食午股份有限公司",
                metric_ids=["total_conversions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
            )

        self.assertEqual(result["semantic"]["profile"], "ecommerce")
        self.assertEqual(
            result["semantic"]["profile_resolution"],
            "tenant_registry.ec",
        )

    def test_generic_query_validates_date_range_before_bigquery(self):
        with self.assertRaises(QueryPolicyError) as raised:
            query_ga4_semantic_metrics(
                customer_name="初衣食午股份有限公司",
                metric_ids=["total_sessions"],
                start_date="2026-08-23",
                end_date="2026-08-17",
            )

        self.assertEqual(raised.exception.code, "invalid_date_range")

    def test_multi_metric_request_limit_blocks_all_data_queries(self):
        registry_job = Mock()
        registry_job.result.return_value = [_tenant_row()]
        client = Mock()
        client.query.side_effect = [
            registry_job,
            SimpleNamespace(total_bytes_processed=8),
            SimpleNamespace(total_bytes_processed=8),
        ]
        policy = QueryPolicy(max_bytes_per_job=10, max_bytes_per_request=15)

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            unittest.mock.patch("main.query_policy", policy),
            self.assertRaises(QueryPolicyError) as raised,
        ):
            query_ga4_semantic_metrics(
                customer_name="初衣食午股份有限公司",
                metric_ids=["total_sessions", "total_users"],
                start_date="2026-08-17",
                end_date="2026-08-23",
            )

        self.assertEqual(raised.exception.code, "query_cost_limit_exceeded")
        self.assertEqual(client.query.call_count, 3)

    def test_metric_without_time_dimension_is_marked_all_available_data(self):
        result = semantic_catalog.search(
            "平均回購次數",
            profile="ecommerce",
        )

        metric = next(
            item
            for item in result["metrics"]
            if item["metric_id"] == "avg_repurchase_count"
        )
        self.assertEqual(metric["date_scope"], "all_available_data")

    def test_catalog_rejects_incomplete_date_placeholders(self):
        catalog = copy.deepcopy(semantic_catalog.catalog)
        metric = catalog["profiles"]["ecommerce"]["metrics"]["total_sessions"]
        metric["sql_template"] = metric["sql_template"].replace(
            "DATE 'end_date'",
            "DATE '2026-08-23'",
        )

        with self.assertRaises(ValueError):
            SemanticCatalog(catalog)


if __name__ == "__main__":
    unittest.main()
