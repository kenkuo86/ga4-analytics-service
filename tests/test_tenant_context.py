from __future__ import annotations

import unittest

from query_policy import QueryPolicyError
from semantic_catalog import SemanticCatalogError
from tenant_context import TenantRequestContext
from traffic_summary_report import TrafficSummaryReportError


class TenantContextTests(unittest.TestCase):
    def test_domain_errors_share_unresolved_context_contract(self):
        context = TenantRequestContext.from_customer_name("  Orient Beauty  ")
        errors = (
            QueryPolicyError("policy_error", "policy failed"),
            SemanticCatalogError("catalog_error", "catalog failed"),
            TrafficSummaryReportError(),
        )

        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                error.attach_request_context(context)
                result = error.as_result()
                self.assertEqual(result["requested_name"], "Orient Beauty")
                self.assertIsNone(result["resolved_name"])
                self.assertEqual(result["match_type"], "none")

    def test_resolved_context_is_applied_to_every_domain_error(self):
        context = TenantRequestContext.from_customer_name("Orient Beauty")
        context.resolve_from_tenant(
            {
                "requested_name": "Orient Beauty",
                "resolved_name": "東方美企業",
                "match_type": "alias",
            }
        )
        errors = (
            QueryPolicyError("policy_error", "policy failed"),
            SemanticCatalogError("catalog_error", "catalog failed"),
            TrafficSummaryReportError(),
        )

        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                error.attach_request_context(context)
                result = error.as_result()
                self.assertEqual(result["requested_name"], "Orient Beauty")
                self.assertEqual(result["resolved_name"], "東方美企業")
                self.assertEqual(result["match_type"], "alias")

    def test_outer_boundary_does_not_downgrade_specific_error_context(self):
        error = SemanticCatalogError("catalog_error", "catalog failed")
        error.attach_tenant_context(
            requested_name="東方美",
            resolved_name="東方美企業",
            match_type="partial",
        )

        error.attach_request_context(
            TenantRequestContext.from_customer_name("東方美")
        )

        result = error.as_result()
        self.assertEqual(result["resolved_name"], "東方美企業")
        self.assertEqual(result["match_type"], "partial")


if __name__ == "__main__":
    unittest.main()
