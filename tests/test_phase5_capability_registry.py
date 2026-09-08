from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from capability_registry import capability_registry
from query_policy import QueryPolicyError
from semantic_catalog import SemanticCatalogError


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "capability_eval_cases.json"


class PhaseFiveCapabilityRegistryTests(unittest.TestCase):
    def test_inventory_is_versioned_and_declares_public_tools(self):
        result = capability_registry.resolve()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["registry_version"], "1.0.0")
        self.assertEqual(result["data_access"], "local_metadata_only")
        self.assertFalse(result["selection_token_required"])
        capability_lookup = next(
            capability
            for capability in result["supported"]
            if capability["capability_id"] == "capability_lookup"
        )
        self.assertEqual(capability_lookup["data_source"], "local_metadata")
        self.assertEqual(capability_lookup["tools"], ["get_ga4_capabilities"])
        self.assertEqual(
            result["public_tools"],
            [
                "customer_lookup",
                "list_available_customers",
                "get_ga4_capabilities",
                "search_ga4_metrics",
                "query_ga4",
                "traffic_summary",
            ],
        )
        capability_tools = {
            tool for capability in result["supported"] for tool in capability["tools"]
        }
        self.assertEqual(capability_tools, set(result["public_tools"]))

    def test_empty_or_unclear_request_needs_clarification(self):
        for request in ("", "分析客戶表現"):
            with self.subTest(request=request):
                result = capability_registry.resolve(request)

                self.assertEqual(result["resolution"], "needs_clarification")
                self.assertEqual(result["next_action"]["type"], "ask_user")
                self.assertEqual(result["data_access"], "local_metadata_only")

    def test_connector_behavior_eval_cases(self):
        import main

        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        for case in cases:
            with self.subTest(case=case["case_id"]):
                client_factory = Mock()
                with unittest.mock.patch.object(
                    main,
                    "get_bigquery_client",
                    client_factory,
                ):
                    if case["operation"] == "capability_lookup":
                        result = main.get_ga4_capability_resolution(case["request"])
                        self.assertEqual(
                            result["resolution"], case["expected_resolution"]
                        )
                        self.assertEqual(
                            result["reason_code"], case["expected_reason_code"]
                        )
                        if result["resolution"] == "supported":
                            self.assertTrue(result["metric_candidates"])
                        expected_metric_id = case.get("expected_candidate_metric_id")
                        if expected_metric_id is not None:
                            self.assertIn(
                                expected_metric_id,
                                {
                                    metric["metric_id"]
                                    for metric in result["metric_candidates"]
                                },
                            )
                    else:
                        expected_error = (
                            QueryPolicyError
                            if case["expected_error"] == "date_range_too_large"
                            else SemanticCatalogError
                        )
                        with self.assertRaises(expected_error) as raised:
                            main.query_ga4_semantic_metrics(
                                customer_name="測試客戶",
                                metric_ids=case["metric_ids"],
                                start_date=case["start_date"],
                                end_date=case["end_date"],
                            )
                        self.assertEqual(
                            raised.exception.code,
                            case["expected_error"],
                        )

                self.assertEqual(
                    client_factory.call_count,
                    case["expected_bigquery_calls"],
                )

    def test_connector_eval_contract_maps_resolution_to_next_action(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        capability_cases = [
            case for case in cases if case["operation"] == "capability_lookup"
        ]

        for case in capability_cases:
            with self.subTest(case=case["case_id"]):
                result = capability_registry.resolve(case["request"])
                resolution = case["expected_resolution"]
                action = result["next_action"]

                if resolution == "supported":
                    self.assertEqual(action["type"], "call_tool")
                    self.assertIn(
                        action["tool"],
                        {"search_ga4_metrics", "traffic_summary"},
                    )
                elif resolution == "unsupported":
                    self.assertEqual(action, {"type": "explain_boundary"})
                else:
                    self.assertEqual(action["type"], "ask_user")
                    self.assertTrue(action["question"])

    def test_profile_mismatch_reads_registry_but_not_tenant_data(self):
        import main

        registry_job = Mock()
        registry_job.result.return_value = [
            SimpleNamespace(
                tenant_id="71",
                tenant_name="測試客戶",
                project_id="customer-project",
                status="active",
                ec=False,
            )
        ]
        client = Mock()
        client.query.return_value = registry_job

        with (
            unittest.mock.patch.object(
                main,
                "get_bigquery_client",
                return_value=client,
            ),
            self.assertRaises(SemanticCatalogError) as raised,
        ):
            main.query_ga4_semantic_metrics(
                customer_name="測試客戶",
                metric_ids=["aov"],
                start_date="2026-08-17",
                end_date="2026-08-23",
            )

        self.assertEqual(raised.exception.code, "unsupported_metric")
        self.assertEqual(client.query.call_count, 1)

    def test_phase_five_test_import_does_not_initialize_oauth_runtime(self):
        repo_root = Path(__file__).parents[1]
        code = """
import os
import sys

for key in (
    "AUTH_MODE",
    "OAUTH_ISSUER_URL",
    "MCP_PUBLIC_URL",
    "OAUTH_ALLOWED_EMAILS",
    "MCP_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "MCP_TOKEN_SIGNING_PRIVATE_KEY",
):
    os.environ.pop(key, None)

sys.path.insert(0, "tests")
import test_phase5_capability_registry
assert "main" not in sys.modules
import test_oauth_flow
assert test_oauth_flow.oauth_runtime.provider is not None
"""
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_tool_descriptions_come_from_registry(self):
        for tool_name in capability_registry.public_tool_names():
            with self.subTest(tool=tool_name):
                description = capability_registry.tool_description(tool_name)
                self.assertTrue(description)
                self.assertNotIn("tenant_id input", description)


if __name__ == "__main__":
    unittest.main()
