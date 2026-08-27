from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from main import TenantResolutionError, get_customer_status, get_tenant_config


def _row(**overrides):
    values = {
        "tenant_id": "5",
        "tenant_name": "維肯媒體部落格",
        "project_id": "my-ga4-project",
        "status": "active",
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
    def test_active_customer_resolves_to_ga4_mar(self):
        client = _client_with_rows([_row()])

        tenant = get_tenant_config(client, "  維肯媒體部落格  ")

        self.assertEqual(tenant["tenant_id"], "5")
        self.assertEqual(tenant["project_id"], "my-ga4-project")
        self.assertEqual(tenant["dataset_id"], "ga4_mar")
        _, kwargs = client.query.call_args
        parameter = kwargs["job_config"].query_parameters[0]
        self.assertEqual(parameter.name, "customer_name")
        self.assertEqual(parameter.value, "維肯媒體部落格")

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


if __name__ == "__main__":
    unittest.main()
