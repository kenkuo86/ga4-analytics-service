from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from main import query_ga4_semantic_metrics
from query_policy import QueryPolicy, QueryPolicyError
from semantic_catalog import SemanticCatalog, SemanticCatalogError, semantic_catalog


def _tenant_row(*, ec: bool | None = False, **overrides):
    values = {
        "tenant_id": "71",
        "tenant_name": "初衣食午股份有限公司",
        "project_id": "customer-project",
        "status": "active",
        "ec": ec,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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

    def test_publishability_preflight_rejects_unknown_metric_locally(self):
        with self.assertRaises(SemanticCatalogError) as raised:
            semantic_catalog.find_publishable_profiles(["invented_roas"])

        self.assertEqual(raised.exception.code, "unsupported_metric")

    def test_publishability_preflight_finds_common_profiles(self):
        profiles = semantic_catalog.find_publishable_profiles(
            ["total_sessions", "total_users"]
        )

        self.assertEqual(profiles, ["non_ecommerce", "ecommerce"])

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
        self.assertNotIn("query_provenance", result)
        semantic_sql = client.query.call_args_list[2].args[0]
        self.assertIn("`customer-project.ga4_mar.mar_ga_sessions`", semantic_sql)
        execution_config = client.query.call_args_list[2].kwargs["job_config"]
        self.assertEqual(execution_config.maximum_bytes_billed, 2_000_000_000)
        self.assertTrue(execution_config.use_query_cache)

    def test_generic_query_preserves_alias_resolution_context(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _tenant_row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        metric_job = Mock()
        metric_job.result.return_value = [{"total_sessions": 123}]
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, metric_job]

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = query_ga4_semantic_metrics(
                customer_name="Orient Beauty",
                metric_ids=["total_sessions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
            )

        self.assertEqual(
            result["tenant"],
            {
                "tenant_id": "71",
                "tenant_name": "東方美企業",
                "requested_name": "Orient Beauty",
                "resolved_name": "東方美企業",
                "match_type": "alias",
            },
        )

    def test_generic_query_policy_preflight_preserves_alias_resolution_context(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _tenant_row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
            )
        ]
        client = Mock()
        client.query.side_effect = [
            registry_job,
            SimpleNamespace(total_bytes_processed=2_000_000_001),
        ]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(QueryPolicyError) as raised,
        ):
            query_ga4_semantic_metrics(
                customer_name="Orient Beauty",
                metric_ids=["total_sessions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
            )

        result = raised.exception.as_result()
        self.assertEqual(result["status"], "query_cost_limit_exceeded")
        self.assertEqual(result["requested_name"], "Orient Beauty")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "alias")

    def test_generic_query_policy_execution_preserves_alias_resolution_context(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _tenant_row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
            )
        ]
        client = Mock()
        client.query.side_effect = [registry_job, SimpleNamespace(total_bytes_processed=1)]
        policy_error = QueryPolicyError("query_timeout", "temporary failure")

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            unittest.mock.patch(
                "query_policy.QueryPolicy.execute",
                side_effect=policy_error,
            ),
            self.assertRaises(QueryPolicyError) as raised,
        ):
            query_ga4_semantic_metrics(
                customer_name="Orient Beauty",
                metric_ids=["total_sessions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
            )

        result = raised.exception.as_result()
        self.assertEqual(result["status"], "query_timeout")
        self.assertEqual(result["requested_name"], "Orient Beauty")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "alias")

    def test_multi_metric_query_returns_ordered_query_provenance_on_request(self):
        registry_job = Mock()
        registry_job.result.return_value = [_tenant_row()]
        dry_run_jobs = [
            SimpleNamespace(total_bytes_processed=1_000_000),
            SimpleNamespace(total_bytes_processed=2_000_000),
        ]
        metric_jobs = []
        for job_id, rows, bytes_processed, bytes_billed in (
            (
                "job-sessions",
                [{"total_sessions": 123}],
                3_000_000,
                2_000_000,
            ),
            (
                "job-users",
                [{"total_users": 45}],
                4_000_000,
                3_000_000,
            ),
        ):
            job = Mock()
            job.job_id = job_id
            job.cache_hit = False
            job.total_bytes_processed = bytes_processed
            job.total_bytes_billed = bytes_billed
            job.result.return_value = rows
            metric_jobs.append(job)

        client = Mock()
        client.query.side_effect = [
            registry_job,
            *dry_run_jobs,
            *metric_jobs,
        ]

        with unittest.mock.patch("main.get_bigquery_client", return_value=client):
            result = query_ga4_semantic_metrics(
                customer_name="初衣食午股份有限公司",
                metric_ids=["total_sessions", "total_users"],
                start_date="2026-08-17",
                end_date="2026-08-23",
                include_query=True,
            )

        provenance = result["query_provenance"]
        self.assertEqual(provenance["schema_version"], "1.0.0")
        self.assertEqual(
            [query["metric_id"] for query in provenance["queries"]],
            ["total_sessions", "total_users"],
        )
        first_query = provenance["queries"][0]
        self.assertIn("@start_date", first_query["sql"])
        self.assertIn("@end_date", first_query["sql"])
        self.assertEqual(
            first_query["parameters"],
            [
                {"name": "start_date", "type": "DATE", "value": "2026-08-17"},
                {"name": "end_date", "type": "DATE", "value": "2026-08-23"},
            ],
        )
        self.assertEqual(first_query["job_id"], "job-sessions")
        self.assertFalse(first_query["cache_hit"])
        self.assertEqual(first_query["bytes_processed"], 3_000_000)
        self.assertEqual(first_query["bytes_billed"], 2_000_000)
        self.assertEqual(first_query["catalog_version"], "1.1.0")

    def test_failed_metric_query_keeps_structured_provenance(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            _tenant_row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        failed_job = Mock()
        failed_job.job_id = "job-failed"
        failed_job.cache_hit = False
        failed_job.total_bytes_processed = 5_000_000
        failed_job.total_bytes_billed = 5_000_000
        failed_job.result.side_effect = RuntimeError("query failed")
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, failed_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(SemanticCatalogError) as raised,
        ):
            query_ga4_semantic_metrics(
                customer_name="Orient Beauty",
                metric_ids=["total_sessions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
                include_query=True,
            )

        self.assertEqual(raised.exception.code, "data_unavailable")
        result = raised.exception.as_result()
        self.assertEqual(result["requested_name"], "Orient Beauty")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "alias")
        provenance = result["details"]["query_provenance"]
        self.assertEqual(provenance["queries"][0]["status"], "failed")
        self.assertEqual(provenance["queries"][0]["job_id"], "job-failed")
        self.assertEqual(provenance["queries"][0]["bytes_billed"], 5_000_000)

    def test_failed_metric_row_iteration_keeps_returned_job_metadata(self):
        class FailingRows:
            def __init__(self):
                self.returned_first_row = False

            def __iter__(self):
                return self

            def __next__(self):
                if not self.returned_first_row:
                    self.returned_first_row = True
                    return {"total_sessions": 123}
                raise RuntimeError("next page failed")

        registry_job = Mock()
        registry_job.result.return_value = [_tenant_row()]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        failed_job = Mock()
        failed_job.job_id = "job-page-failed"
        failed_job.cache_hit = True
        failed_job.total_bytes_processed = 7_000_000
        failed_job.total_bytes_billed = 6_000_000
        failed_job.result.return_value = FailingRows()
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, failed_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(SemanticCatalogError) as raised,
        ):
            query_ga4_semantic_metrics(
                customer_name="初衣食午股份有限公司",
                metric_ids=["total_sessions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
                include_query=True,
            )

        query = raised.exception.as_result()["details"]["query_provenance"][
            "queries"
        ][0]
        self.assertEqual(query["status"], "failed")
        self.assertEqual(query["job_id"], "job-page-failed")
        self.assertTrue(query["cache_hit"])
        self.assertEqual(query["bytes_processed"], 7_000_000)
        self.assertEqual(query["bytes_billed"], 6_000_000)

    def test_failed_alias_metric_row_serialization_preserves_context(self):
        class BadRow:
            def items(self):
                raise RuntimeError("row serialization failed")

        registry_job = Mock()
        registry_job.result.return_value = [
            _tenant_row(
                tenant_name="東方美企業",
                aliases="Orient Beauty",
                match_type="alias",
            )
        ]
        dry_run_job = SimpleNamespace(total_bytes_processed=1_000_000)
        metric_job = Mock()
        metric_job.job_id = "job-serialization-failed"
        metric_job.cache_hit = False
        metric_job.total_bytes_processed = 5_000_000
        metric_job.total_bytes_billed = 5_000_000
        metric_job.result.return_value = [BadRow()]
        client = Mock()
        client.query.side_effect = [registry_job, dry_run_job, metric_job]

        with (
            unittest.mock.patch("main.get_bigquery_client", return_value=client),
            self.assertRaises(SemanticCatalogError) as raised,
        ):
            query_ga4_semantic_metrics(
                customer_name="Orient Beauty",
                metric_ids=["total_sessions"],
                start_date="2026-08-17",
                end_date="2026-08-23",
                include_query=True,
            )

        result = raised.exception.as_result()
        self.assertEqual(result["status"], "data_unavailable")
        self.assertEqual(result["requested_name"], "Orient Beauty")
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "alias")
        query = result["details"]["query_provenance"]["queries"][0]
        self.assertEqual(query["status"], "failed")
        self.assertEqual(query["job_id"], "job-serialization-failed")

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
