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
            "GA4 sessions 2026-01-01 before 2026-09-01",
            "GA4 sessions 2026-01-01 之前 2026-09-01",
            "GA4 sessions 2026-01-01 foo 2026-09-01",
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
        for malformed_date in (
            "2026/009/01",
            "2026-009-01",
            "2026--09-01",
            "2026-xx-01",
        ):
            with self.subTest(malformed_date=malformed_date):
                result = resolve_period_intent(
                    f"GA4 sessions {malformed_date}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "invalid_period")
                self.assertEqual(result.reason_code, "invalid_date_format")

    def test_year_shaped_filter_identifiers_are_not_dates(self):
        for filter_value in (
            "summer2026-sale-us",
            "2026-sale-us",
            "2026-q1-sales",
            "2026-09-sale",
            "2026-summer-sale-01",
            "product2026-offer-tw",
        ):
            with self.subTest(filter_value=filter_value):
                result = resolve_period_intent(
                    f"GA4 sessions for campaign {filter_value}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "none")
                self.assertEqual(result.requested_days, 0)
                self.assertFalse(result.phrase_matches)

    def test_explicit_date_context_rejects_nonnumeric_components(self):
        for request in (
            "GA4 sessions from 2026-abc-01",
            "GA4 sessions date is 2026-abc-01",
            "GA4 sessions today through 2026-abc-01",
        ):
            with self.subTest(request=request):
                result = resolve_period_intent(
                    request,
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "invalid_period")
                self.assertEqual(result.reason_code, "invalid_date_format")

        filter_result = resolve_period_intent(
            "GA4 sessions attributed to 2026-09-sale",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        self.assertEqual(filter_result.outcome, "none")

    def test_explicit_filter_values_are_excluded_before_period_parsing(self):
        filter_only_requests = (
            "GA4 sessions for campaign 2026-09-01",
            "GA4 sessions for campaign is 2026-09-01",
            "GA4 sessions for campaign equals 2026-09-01",
            "GA4 sessions for campaign = 2026-09-01",
            "GA4 sessions campaign: 2026-09-01",
            "GA4 sessions for source 2026/09/01",
            "GA4 sessions for source is 2026/09/01",
            "GA4 sessions from 2026-09-sale campaign",
            "GA4 sessions 針對活動 2026-09-01",
        )
        for request in filter_only_requests:
            with self.subTest(request=request):
                result = resolve_period_intent(
                    request,
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )
                self.assertEqual(result.outcome, "none")
                self.assertEqual(result.requested_days, 0)

        filter_and_period = resolve_period_intent(
            "GA4 sessions for campaign 2026-01-01, today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        self.assertEqual(filter_and_period.outcome, "resolved")
        self.assertEqual(filter_and_period.requested_days, 1)
        self.assertEqual(len(filter_and_period.explicit_periods), 1)
        self.assertEqual(filter_and_period.explicit_periods[0].phrase, "today")

        metric_and_period = resolve_period_intent(
            "GA4 landing page today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        self.assertEqual(metric_and_period.outcome, "resolved")
        self.assertEqual(metric_and_period.requested_days, 1)

    def test_mixed_period_endpoints_use_the_same_connector_grammar(self):
        resolved = resolve_period_intent(
            "GA4 sessions 2026-01-01 through today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        unsupported = resolve_period_intent(
            "GA4 sessions 2026-01-01 before today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        missing_connector = resolve_period_intent(
            "GA4 sessions 2026-01-01 today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        non_endpoint_range = resolve_period_intent(
            "GA4 sessions past 7 days through today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(resolved.outcome, "resolved")
        self.assertEqual(resolved.requested_days, 257)
        self.assertEqual(len(resolved.explicit_periods), 1)
        self.assertEqual(
            resolved.explicit_periods[0].start_date,
            date(2026, 1, 1),
        )
        self.assertEqual(resolved.explicit_periods[0].end_date, date(2026, 9, 14))
        for result in (unsupported, missing_connector, non_endpoint_range):
            with self.subTest(result=result):
                self.assertEqual(result.outcome, "needs_clarification")
                self.assertEqual(
                    result.reason_code,
                    "unsupported_date_range_connector",
                )
                self.assertEqual(result.requested_days, 0)

    def test_chained_range_connectors_require_clarification(self):
        for request in (
            "GA4 sessions yesterday through today through 2026-01-01",
            "GA4 sessions 2026-01-01 and through today",
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

    def test_english_relative_phrases_require_a_leading_boundary(self):
        for phrase in ("compast 7 days", "xrecent 7 days"):
            with self.subTest(phrase=phrase):
                result = resolve_period_intent(
                    f"GA4 sessions {phrase}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "needs_clarification")
                self.assertEqual(result.reason_code, "ambiguous_period")
                self.assertEqual(result.requested_days, 0)

    def test_unconsumed_period_residue_never_resolves_as_no_period(self):
        for phrase in (
            "pas 7 days",
            "past 7 fortnights",
            "past 7",
            "7 days",
        ):
            with self.subTest(phrase=phrase):
                result = resolve_period_intent(
                    f"GA4 sessions {phrase}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result.outcome, "needs_clarification")
                self.assertEqual(result.reason_code, "ambiguous_period")
                self.assertEqual(result.requested_days, 0)


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
                self.assertEqual(
                    len(result["period"]["explicit_periods"]),
                    case.get(
                        "expected_explicit_periods",
                        len(result["period"]["explicit_periods"]),
                    ),
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

    def test_mixed_endpoint_range_is_not_split_and_uses_full_duration(self):
        allowed = capability_registry.resolve(
            "GA4 sessions from 2026-09-01 through today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        rejected = capability_registry.resolve(
            "GA4 sessions from 2026-01-01 through today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(allowed["resolution"], "supported")
        self.assertEqual(allowed["reason_code"], "ga4_semantic_metric")
        self.assertEqual(allowed["period"]["requested_days"], 14)
        self.assertEqual(rejected["resolution"], "needs_clarification")
        self.assertEqual(rejected["reason_code"], "date_range_too_large")
        self.assertEqual(rejected["period"]["requested_days"], 257)

    def test_year_shaped_filter_identifier_does_not_block_capability_resolution(self):
        for request in (
            "GA4 sessions for campaign summer2026-sale-us",
            "GA4 sessions attributed to 2026-09-sale",
            "GA4 sessions for campaign 2026-09-01",
            "GA4 sessions campaign: 2026-09-01",
            "GA4 sessions for source 2026/09/01",
            "GA4 sessions from 2026-09-sale campaign",
        ):
            with self.subTest(request=request):
                result = capability_registry.resolve(
                    request,
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result["resolution"], "supported")
                self.assertEqual(result["reason_code"], "ga4_semantic_metric")
                self.assertEqual(result["period"]["outcome"], "none")

        malformed = capability_registry.resolve(
            "GA4 sessions date is 2026-abc-01",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        self.assertEqual(malformed["resolution"], "needs_clarification")
        self.assertEqual(malformed["reason_code"], "invalid_period")

        filter_and_period = capability_registry.resolve(
            "GA4 sessions for campaign 2026-01-01, today",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        self.assertEqual(filter_and_period["resolution"], "supported")
        self.assertEqual(filter_and_period["period"]["requested_days"], 1)

    def test_punctuation_joiner_and_multi_metric_clauses_keep_periods_independent(self):
        multiple_ranges = capability_registry.resolve(
            "GA4 sessions 2026-08-01 to 2026-08-02, and " "2026-09-01 to 2026-09-02",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        multiple_metrics = capability_registry.resolve(
            "GA4 sessions today and GA4 users yesterday",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        comma_metrics = capability_registry.resolve(
            "GA4 sessions today, GA4 users yesterday",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        semicolon_metrics = capability_registry.resolve(
            "GA4 sessions today; GA4 users yesterday",
            policy=_policy(),
            today=date(2026, 9, 14),
        )
        attributed_metrics = capability_registry.resolve(
            "GA4 sessions today and GA4 users attributed to campaign yesterday",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        for result, expected_days in (
            (multiple_ranges, 4),
            (multiple_metrics, 2),
            (comma_metrics, 2),
            (semicolon_metrics, 2),
            (attributed_metrics, 2),
        ):
            with self.subTest(result=result):
                self.assertEqual(result["resolution"], "supported")
                self.assertEqual(result["reason_code"], "ga4_semantic_metric")
                self.assertEqual(result["period"]["requested_days"], expected_days)
                self.assertEqual(len(result["period"]["explicit_periods"]), 2)

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
        for malformed_date in (
            "2026/009/01",
            "2026-009-01",
            "2026--09-01",
            "2026-xx-01",
        ):
            with self.subTest(malformed_date=malformed_date):
                result = capability_registry.resolve(
                    f"GA4 sessions {malformed_date}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result["resolution"], "needs_clarification")
                self.assertEqual(result["reason_code"], "invalid_period")
                self.assertEqual(result["period"]["reason_code"], "invalid_date_format")

    def test_embedded_english_relative_phrase_is_not_accepted(self):
        for phrase in ("compast 7 days", "xrecent 7 days"):
            with self.subTest(phrase=phrase):
                result = capability_registry.resolve(
                    f"GA4 sessions {phrase}",
                    policy=_policy(),
                    today=date(2026, 9, 14),
                )

                self.assertEqual(result["resolution"], "needs_clarification")
                self.assertEqual(result["reason_code"], "ambiguous_period")
                self.assertEqual(result["period"]["requested_days"], 0)

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

    def test_traffic_summary_rejects_user_selected_comparison_range(self):
        result = capability_registry.resolve(
            "traffic summary, 2026-08-01 to 2026-08-07 and " "2026-07-01 to 2026-07-07",
            policy=_policy(),
            today=date(2026, 9, 14),
        )

        self.assertEqual(result["resolution"], "needs_clarification")
        self.assertEqual(result["reason_code"], "traffic_comparison_not_representable")
        self.assertEqual(result["period"]["requested_days"], 14)
        self.assertEqual(len(result["period"]["explicit_periods"]), 2)
        self.assertEqual(len(result["period"]["implicit_periods"]), 0)

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
