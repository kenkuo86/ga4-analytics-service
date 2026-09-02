from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.validate_all_tenants import (
    get_active_tenants,
    get_gcloud_source_credentials,
    validate_tenants,
)


def _tenant(**overrides):
    values = {
        "tenant_id": "5",
        "tenant_name": "維肯媒體部落格",
        "project_id": "customer-project",
        "dataset_id": "ga4_mar",
        "profile": "non_ecommerce",
    }
    values.update(overrides)
    return values


class ValidateAllTenantsTests(unittest.TestCase):
    @patch("scripts.validate_all_tenants.subprocess.run")
    def test_gcloud_source_token_stays_in_memory(self, run):
        run.return_value = SimpleNamespace(stdout="temporary-token\n")

        credentials = get_gcloud_source_credentials()

        self.assertEqual(credentials.token, "temporary-token")
        run.assert_called_once_with(
            ["gcloud", "auth", "print-access-token"],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_registry_is_read_with_one_query_and_maps_profiles(self):
        registry_job = Mock()
        registry_job.result.return_value = [
            SimpleNamespace(
                tenant_id=5,
                tenant_name="維肯媒體部落格",
                project_id="project-a",
                ec=False,
            ),
            SimpleNamespace(
                tenant_id=6,
                tenant_name="電商客戶",
                project_id="project-b",
                ec=True,
            ),
        ]
        client = Mock()
        client.query.return_value = registry_job

        tenants = get_active_tenants(client, "registry-project.ops.tenants")

        self.assertEqual(client.query.call_count, 1)
        self.assertEqual(tenants[0]["profile"], "non_ecommerce")
        self.assertEqual(tenants[1]["profile"], "ecommerce")
        sql = client.query.call_args.args[0]
        self.assertIn("LOWER(TRIM(status)) = 'active'", sql)
        self.assertIn("NULLIF(TRIM(project_id), '') IS NOT NULL", sql)

    def test_every_tenant_query_is_a_dry_run(self):
        dry_run_job = SimpleNamespace(total_bytes_processed=123)
        client = Mock()
        client.query.return_value = dry_run_job

        report = validate_tenants(
            client,
            [_tenant(), _tenant(tenant_id="6", project_id="other-project")],
            start_date=date(2026, 8, 17),
            end_date=date(2026, 8, 23),
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(client.query.call_count, 2)
        for call in client.query.call_args_list:
            self.assertTrue(call.kwargs["job_config"].dry_run)
            self.assertFalse(call.kwargs["job_config"].use_query_cache)
        self.assertEqual(report["cost_safety"]["registry_real_query_count"], 1)
        self.assertEqual(report["cost_safety"]["tenant_real_query_count"], 0)
        self.assertEqual(report["cost_safety"]["tenant_dry_run_count"], 2)
        self.assertFalse(report["cost_safety"]["dry_runs_are_billed"])

    def test_failure_is_reported_and_remaining_tenants_continue(self):
        client = Mock()
        client.query.side_effect = [
            PermissionError("missing Data Viewer"),
            SimpleNamespace(total_bytes_processed=456),
        ]

        report = validate_tenants(
            client,
            [_tenant(), _tenant(tenant_id="6", project_id="other-project")],
            start_date=date(2026, 8, 17),
            end_date=date(2026, 8, 23),
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failed_tenant_count"], 1)
        self.assertEqual(report["passed_tenant_count"], 1)
        self.assertEqual(client.query.call_count, 2)
        self.assertIn(
            "missing Data Viewer",
            report["tenants"][0]["metrics"][0]["error"],
        )

    def test_invalid_project_is_reported_without_querying_tenant_data(self):
        client = Mock()

        report = validate_tenants(
            client,
            [_tenant(project_id="invalid.project")],
            start_date=date(2026, 8, 17),
            end_date=date(2026, 8, 23),
        )

        self.assertEqual(report["status"], "failed")
        client.query.assert_not_called()
        self.assertIn(
            "project_id",
            report["tenants"][0]["metrics"][0]["error"],
        )


if __name__ == "__main__":
    unittest.main()
