from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from capability_registry import capability_registry
from period_contract import (
    PERIOD_PHRASE_CONTRACT_VERSION,
    period_contract_inventory,
    period_instruction,
    resolve_period_intent,
)
from query_policy import QueryPolicy, QueryPolicyError


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "phase10_period_eval_cases.json"


def _policy(*, max_days: int = 90, time_zone: str = "Asia/Taipei") -> QueryPolicy:
    return QueryPolicy(
        max_date_range_days=max_days,
        time_zone=time_zone,
    )


class PhaseTenPeriodContractTests(unittest.TestCase):
    def test_versioned_fixture_covers_contract_window_kinds_and_outcomes(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        inventory = period_contract_inventory()

        self.assertEqual(fixture["contract_version"], PERIOD_PHRASE_CONTRACT_VERSION)
        self.assertEqual(inventory["version"], fixture["contract_version"])
        fixture_kinds = {
            case["expected_window_kind"]
            for case in fixture["cases"]
            if "expected_window_kind" in case
        }
        contract_kinds = {
            family["window_kind"] for family in inventory["phrase_families"]
        }
        self.assertTrue(contract_kinds.issubset(fixture_kinds))
        self.assertIn("fixed_previous_comparison", fixture_kinds)
        self.assertEqual(
            set(inventory["outcomes"]),
            {
                "resolved",
                "needs_clarification",
                "invalid_period",
            },
        )

        instructions = period_instruction(_policy())
        for family in inventory["phrase_families"]:
            with self.subTest(window_kind=family["window_kind"]):
                self.assertIn(family["window_kind"], instructions)
                for phrase_template in family["phrases"]:
                    self.assertIn(phrase_template, instructions)

        fixture_phrases = [case["phrase"].casefold() for case in fixture["cases"]]
        for family in inventory["phrase_families"]:
            aliases = family.get("aliases", {})
            for alias_group in aliases.values():
                values = alias_group
                if isinstance(alias_group, dict):
                    values = [
                        alias
                        for quantity_aliases in alias_group.values()
                        for alias, _quantity in quantity_aliases
                    ]
                for alias in values:
                    with self.subTest(alias=alias):
                        self.assertTrue(
                            any(
                                alias.casefold() in phrase for phrase in fixture_phrases
                            ),
                            f"fixture is missing contract alias: {alias}",
                        )
            for alias, _canonical in family.get("fixed_aliases", []):
                with self.subTest(alias=alias):
                    self.assertTrue(
                        any(alias.casefold() in phrase for phrase in fixture_phrases),
                        f"fixture is missing fixed contract alias: {alias}",
                    )

    def test_contract_fixture_resolves_every_case(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        policy = _policy()

        for case in fixture["cases"]:
            with self.subTest(case=case["case_id"]):
                result = resolve_period_intent(
                    case["phrase"],
                    policy=policy,
                    today=date.fromisoformat(case["today"]),
                )
                self.assertEqual(result.outcome, case["expected_outcome"])
                self.assertEqual(
                    result.requested_days,
                    case.get("expected_requested_days", result.requested_days),
                )
                if "expected_window_kind" in case:
                    self.assertTrue(result.phrase_matches)
                    self.assertEqual(
                        result.phrase_matches[0].window_kind,
                        case["expected_window_kind"],
                    )
                if "expected_start_date" in case:
                    self.assertEqual(
                        result.explicit_periods[0].start_date.isoformat(),
                        case["expected_start_date"],
                    )
                    self.assertEqual(
                        result.explicit_periods[0].end_date.isoformat(),
                        case["expected_end_date"],
                    )
                if "expected_reason_code" in case:
                    self.assertEqual(result.reason_code, case["expected_reason_code"])

    def test_explicit_period_union_deduplicates_overlap_and_adjacent_ranges(self):
        result = resolve_period_intent(
            "2026-06-01 to 2026-06-30 and 2026-06-15 to 2026-07-15",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(result.outcome, "resolved")
        self.assertEqual(len(result.explicit_periods), 2)
        self.assertEqual(result.requested_days, 45)

    def test_traffic_previous_period_is_implicit_and_not_counted(self):
        result = resolve_period_intent(
            "traffic summary 2026-06-17 to 2026-09-14 compare with the previous period",
            policy=_policy(),
            today=date(2026, 9, 14),
            include_previous_comparison=True,
        )

        self.assertEqual(result.outcome, "resolved")
        self.assertEqual(result.requested_days, 90)
        self.assertTrue(result.comparison_modifier)
        self.assertEqual(len(result.explicit_periods), 1)
        self.assertEqual(len(result.implicit_periods), 1)
        self.assertEqual(
            result.implicit_periods[0].as_dict(),
            {
                "phrase": "previous period",
                "window_kind": "fixed_previous_comparison",
                "start_date": "2026-03-19",
                "end_date": "2026-06-16",
                "days": 90,
                "source": "implicit",
            },
        )

    def test_traffic_previous_period_still_checks_earliest_date(self):
        result = resolve_period_intent(
            "traffic summary 2020-10-15 to 2021-01-12",
            policy=_policy(),
            today=date(2026, 9, 14),
            include_previous_comparison=True,
        )

        self.assertEqual(result.outcome, "invalid_period")
        self.assertEqual(result.reason_code, "date_before_available_range")

    def test_iso_dates_require_complete_tokens(self):
        for malformed_date in (
            "12026-09-01",
            "2026-09-011",
            "2026-09-01abc",
        ):
            with self.subTest(malformed_date=malformed_date):
                result = resolve_period_intent(
                    f"GA4 sessions {malformed_date}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "invalid_period")
                self.assertEqual(result.reason_code, "invalid_date_format")
                self.assertEqual(
                    result.phrase_matches[0].reason_code,
                    "invalid_date_format",
                )

    def test_unsupported_range_connectors_do_not_count_endpoints_independently(self):
        for request in (
            "GA4 sessions 2026-01-01 until 2026-09-01",
            "GA4 sessions 2026-01-01 截至 2026-09-01",
        ):
            with self.subTest(request=request):
                result = resolve_period_intent(
                    request,
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "needs_clarification")
                self.assertEqual(
                    result.reason_code,
                    "unsupported_date_range_connector",
                )
                self.assertEqual(result.requested_days, 0)

    def test_fractional_period_quantity_is_not_ignored(self):
        for phrase in ("past 100.5 days", "過去 100.5 天"):
            with self.subTest(phrase=phrase):
                result = resolve_period_intent(
                    f"GA4 sessions {phrase}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "invalid_period")
                self.assertEqual(result.reason_code, "fractional_period_quantity")

    def test_date_component_width_over_two_digits_is_invalid(self):
        for malformed_date in ("2026/009/01", "2026-009-01"):
            with self.subTest(malformed_date=malformed_date):
                result = resolve_period_intent(
                    f"GA4 sessions {malformed_date}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "invalid_period")
                self.assertEqual(result.reason_code, "invalid_date_format")


class PhaseTenCapabilityBoundaryTests(unittest.TestCase):
    def test_versioned_behavior_matrix_keeps_data_calls_at_zero(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        for case in fixture["behavior_cases"]:
            with self.subTest(case=case["case_id"]):
                policy = _policy(max_days=case["max_days"])
                result = capability_registry.resolve(
                    case["request"],
                    policy=policy,
                    today=date.fromisoformat(case["today"]),
                )
                self.assertEqual(result["resolution"], case["expected_resolution"])
                self.assertEqual(result["reason_code"], case["expected_reason_code"])
                self.assertEqual(
                    result["period"]["requested_days"],
                    case["expected_requested_days"],
                )
                self.assertEqual(
                    len(result["period"]["implicit_periods"]),
                    case.get("expected_implicit_periods", 0),
                )
                self.assertEqual(case["expected_bigquery_calls"], 0)

    def test_over_limit_intent_is_rejected_before_metric_tool_choice(self):
        result = capability_registry.resolve(
            "GA4 sessions, 過去三個月",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(result["resolution"], "needs_clarification")
        self.assertEqual(result["reason_code"], "date_range_too_large")
        self.assertEqual(result["period"]["requested_days"], 92)
        self.assertEqual(result["period"]["max_days"], 90)
        self.assertEqual(result["next_action"]["type"], "ask_user")

    def test_exact_active_limit_is_allowed_and_limit_plus_one_is_rejected(self):
        policy = _policy()
        allowed = capability_registry.resolve(
            "GA4 sessions, 2026-06-17 to 2026-09-14",
            policy=policy,
            today=date(2026, 9, 14),
        )
        rejected = capability_registry.resolve(
            "GA4 sessions, 2026-06-16 to 2026-09-14",
            policy=policy,
            today=date(2026, 9, 14),
        )

        self.assertEqual(allowed["resolution"], "supported")
        self.assertEqual(allowed["period"]["requested_days"], 90)
        self.assertEqual(rejected["reason_code"], "date_range_too_large")
        self.assertEqual(rejected["period"]["requested_days"], 91)

    def test_from_to_date_range_is_not_split_as_a_mixed_request(self):
        result = capability_registry.resolve(
            "GA4 sessions from 2026-08-01 to 2026-08-31",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(result["resolution"], "supported")
        self.assertEqual(result["reason_code"], "ga4_semantic_metric")
        self.assertEqual(result["period"]["requested_days"], 31)

    def test_unsupported_range_connector_requires_period_clarification(self):
        for request in (
            "GA4 sessions 2026-01-01 until 2026-09-01",
            "GA4 sessions 2026-01-01 截至 2026-09-01",
        ):
            with self.subTest(request=request):
                result = capability_registry.resolve(
                    request,
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result["resolution"], "needs_clarification")
                self.assertEqual(result["reason_code"], "ambiguous_period")
                self.assertEqual(
                    result["period"]["reason_code"],
                    "unsupported_date_range_connector",
                )
                self.assertEqual(result["period"]["requested_days"], 0)

    def test_non_default_policy_is_reflected_in_metadata_instructions_and_error(self):
        policy = _policy(max_days=31, time_zone="UTC")
        metadata = capability_registry.resolve(None, policy=policy)
        result = capability_registry.resolve(
            "GA4 sessions, 2026-08-14 to 2026-09-14",
            policy=policy,
            today=date(2026, 9, 14),
        )

        self.assertEqual(
            metadata["query_policy"],
            {
                "max_date_range_days": 31,
                "time_zone": "UTC",
            },
        )
        self.assertEqual(result["reason_code"], "date_range_too_large")
        self.assertEqual(result["period"]["max_days"], 31)
        self.assertIn(
            "31", capability_registry.tool_description("query_ga4", policy=policy)
        )
        self.assertIn("UTC", capability_registry.server_instructions(policy=policy))
        self.assertIn("31", metadata["limitations"][-1])

    def test_environment_policy_is_used_by_the_same_contract(self):
        with patch.dict(
            os.environ,
            {
                "GA4_QUERY_MAX_DAYS": "31",
                "GA4_QUERY_TIME_ZONE": "UTC",
            },
            clear=False,
        ):
            policy = QueryPolicy.from_environment()

        metadata = capability_registry.resolve(None, policy=policy)
        allowed = capability_registry.resolve(
            "GA4 sessions, 2026-08-15 to 2026-09-14",
            policy=policy,
            today=date(2026, 9, 14),
        )
        rejected = capability_registry.resolve(
            "GA4 sessions, 2026-08-14 to 2026-09-14",
            policy=policy,
            today=date(2026, 9, 14),
        )

        self.assertEqual(
            metadata["query_policy"],
            {
                "max_date_range_days": 31,
                "time_zone": "UTC",
            },
        )
        self.assertEqual(allowed["resolution"], "supported")
        self.assertEqual(rejected["reason_code"], "date_range_too_large")
        self.assertIn("31", rejected["message"])

    def test_invalid_period_is_not_silently_removed(self):
        result = capability_registry.resolve(
            "GA4 sessions, 2026/09/01",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(result["resolution"], "needs_clarification")
        self.assertEqual(result["reason_code"], "invalid_period")
        self.assertEqual(result["period"]["outcome"], "invalid_period")
        self.assertEqual(
            result["period"]["phrase_matches"][0]["reason_code"],
            "invalid_date_format",
        )

    def test_out_of_range_relative_quantity_returns_structured_period_error(self):
        for period_phrase in (
            "past 1000000000 days",
            "past 1000000000 months",
            "past 1000000000 years",
        ):
            with self.subTest(period_phrase=period_phrase):
                result = capability_registry.resolve(
                    f"GA4 sessions, {period_phrase}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result["resolution"], "needs_clarification")
                self.assertEqual(result["reason_code"], "invalid_period")
                self.assertEqual(result["period"]["outcome"], "invalid_period")
                self.assertEqual(
                    result["period"]["phrase_matches"][0]["reason_code"],
                    "period_quantity_out_of_range",
                )

    def test_fractional_period_quantity_requires_structured_clarification(self):
        for phrase in ("past 100.5 days", "過去 100.5 天"):
            with self.subTest(phrase=phrase):
                result = capability_registry.resolve(
                    f"GA4 sessions, {phrase}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result["resolution"], "needs_clarification")
                self.assertEqual(result["reason_code"], "invalid_period")
                self.assertEqual(
                    result["period"]["reason_code"],
                    "fractional_period_quantity",
                )

    def test_date_component_width_over_two_digits_is_not_silently_accepted(self):
        for malformed_date in ("2026/009/01", "2026-009-01"):
            with self.subTest(malformed_date=malformed_date):
                result = capability_registry.resolve(
                    f"GA4 sessions {malformed_date}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result["resolution"], "needs_clarification")
                self.assertEqual(result["reason_code"], "invalid_period")
                self.assertEqual(result["period"]["reason_code"], "invalid_date_format")

    def test_traffic_comparison_modifier_does_not_add_explicit_days(self):
        result = capability_registry.resolve(
            "traffic summary 2026-06-17 to 2026-09-14 與前期比較",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(result["resolution"], "supported")
        self.assertEqual(result["period"]["requested_days"], 90)
        self.assertEqual(len(result["period"]["explicit_periods"]), 1)
        self.assertEqual(len(result["period"]["implicit_periods"]), 1)

    def test_mixed_date_scope_keeps_only_intent_period_model(self):
        result = capability_registry.resolve(
            "GA4 sessions and aov, past seven days",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(result["resolution"], "supported")
        self.assertEqual(result["period"]["requested_days"], 7)
        self.assertNotIn("effective_scan_periods", result["period"])
        self.assertNotIn("effective_scan_days", result["period"])

    def test_data_tool_date_policy_still_blocks_registry_and_data_queries(self):
        import main

        client = SimpleNamespace()
        with patch.object(
            main, "get_bigquery_client", return_value=client
        ) as get_client:
            with self.assertRaises(QueryPolicyError) as raised:
                main.query_ga4_semantic_metrics(
                    customer_name="測試客戶",
                    metric_ids=["total_sessions"],
                    start_date="2026-06-16",
                    end_date="2026-09-14",
                )

        self.assertEqual(
            getattr(raised.exception, "code", None), "date_range_too_large"
        )
        get_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
