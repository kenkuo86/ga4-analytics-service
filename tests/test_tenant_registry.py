from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scripts.validate_tenant_registry import validate_registry
from tenant_registry import (
    TenantRegistryValidationError,
    normalize_customer_name,
    parse_aliases,
    validate_registry_rows,
)


def _row(**overrides):
    values = {
        "tenant_id": "1",
        "tenant_name": "東方美企業",
        "aliases": "東方美|Orient Beauty",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TenantRegistryTests(unittest.TestCase):
    def test_normalization_matches_trim_nfkc_and_casefold_policy(self):
        self.assertEqual(
            normalize_customer_name("  Ｓｕｎｎｙ Digital  "),
            "sunny digital",
        )

    def test_parse_aliases_ignores_empty_runtime_segments(self):
        self.assertEqual(
            parse_aliases("小太陽| |Sunny Digital||"),
            ["小太陽", "Sunny Digital"],
        )

    def test_valid_registry_rows_report_alias_count(self):
        report = validate_registry_rows(
            [
                _row(),
                _row(
                    tenant_id="2",
                    tenant_name="另一家企業",
                    aliases=None,
                ),
            ]
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["tenant_count"], 2)
        self.assertEqual(report["validated_tenant_count"], 2)
        self.assertEqual(report["skipped_unnamed_tenant_count"], 0)
        self.assertEqual(report["alias_count"], 2)

    def test_nameless_rows_are_reported_and_skipped_from_alias_validation(self):
        report = validate_registry_rows(
            [
                _row(),
                _row(
                    tenant_id="2",
                    tenant_name=None,
                    aliases="東方美企業||公司",
                ),
                _row(
                    tenant_id="3",
                    tenant_name="   ",
                    aliases="Orient Beauty",
                ),
            ]
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["tenant_count"], 3)
        self.assertEqual(report["validated_tenant_count"], 1)
        self.assertEqual(report["skipped_unnamed_tenant_count"], 2)
        self.assertEqual(report["alias_count"], 2)

    def test_duplicate_alias_across_tenants_is_rejected(self):
        with self.assertRaises(TenantRegistryValidationError) as raised:
            validate_registry_rows(
                [
                    _row(),
                    _row(
                        tenant_id="2",
                        tenant_name="另一家企業",
                        aliases="orient beauty",
                    ),
                ]
            )

        self.assertIn(
            "duplicate_alias_across_tenants",
            {issue["code"] for issue in raised.exception.issues},
        )

    def test_duplicate_physical_rows_with_same_tenant_id_are_rejected(self):
        with self.assertRaises(TenantRegistryValidationError) as raised:
            validate_registry_rows([_row(), _row()])

        issue_codes = {issue["code"] for issue in raised.exception.issues}
        self.assertIn("duplicate_formal_name", issue_codes)
        self.assertIn("duplicate_alias_across_tenants", issue_codes)

    def test_alias_formal_name_collision_is_rejected(self):
        with self.assertRaises(TenantRegistryValidationError) as raised:
            validate_registry_rows(
                [
                    _row(),
                    _row(
                        tenant_id="2",
                        tenant_name="另一家企業",
                        aliases="東方美企業",
                    ),
                ]
            )

        self.assertIn(
            "alias_conflicts_with_formal_name",
            {issue["code"] for issue in raised.exception.issues},
        )

    def test_duplicate_and_generic_aliases_are_rejected(self):
        with self.assertRaises(TenantRegistryValidationError) as raised:
            validate_registry_rows(
                [
                    _row(aliases="Brand|ｂｒａｎｄ|公司"),
                ]
            )

        issue_codes = {issue["code"] for issue in raised.exception.issues}
        self.assertIn("duplicate_alias_within_tenant", issue_codes)
        self.assertIn("generic_alias", issue_codes)

    def test_empty_alias_segment_is_rejected_at_validation_stage(self):
        with self.assertRaises(TenantRegistryValidationError) as raised:
            validate_registry_rows([_row(aliases="Brand||Other")])

        self.assertIn(
            "empty_alias",
            {issue["code"] for issue in raised.exception.issues},
        )

    def test_rollout_validation_reads_registry_once_and_includes_aliases(self):
        job = Mock()
        job.result.return_value = [_row()]
        client = Mock()
        client.query.return_value = job

        report = validate_registry(client, "registry-project.ops.tenants")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["registry_query_count"], 1)
        self.assertEqual(client.query.call_count, 1)
        self.assertIn("aliases", client.query.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
