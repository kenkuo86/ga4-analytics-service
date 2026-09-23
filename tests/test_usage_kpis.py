"""Offline acceptance tests for the Phase 11.6 KPI contract."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usage_kpis import (
    ActivationLedger,
    HistoryCoverage,
    KPIInputError,
    build_kpi_view,
    build_weekly_summary,
    deduplicate_events,
    update_activation_ledger,
)


ROOT = Path(__file__).resolve().parents[1]


def user(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def event(index: int, when: str, *, who: str | None = "alice", kind: str = "analytics",
          status: str = "success", transport: str = "mcp", tool: str | None = "traffic_summary",
          resolution: str = "supported", error_code=None, goal: str = "unknown",
          subject: str = "traffic", requested_days=7, period_type: str = "explicit_range",
          comparison: str = "previous_period", metrics=None, dimensions=None,
          identity: str | None = "verified", host: str = "claude", tenant_id="5",
          authorization: str = "allowed", latency: int = 100) -> dict:
    return {
        "schema_version": "1.0",
        "event_name": "analytics_request_completed",
        "event_time": when,
        "interaction_id": f"00000000-0000-4000-8000-{index:012d}",
        "transport": transport,
        "request_kind": kind,
        "user_id": user(who) if who is not None and identity == "verified" else None,
        "identity_status": identity,
        "host": host,
        "tenant_id": tenant_id,
        "authorization_scope_ref": "department-active-tenants-v1",
        "authorization_result": authorization,
        "tool_name": tool,
        "request_summary": None,
        "request_summary_source": "unavailable",
        "analysis_goal": goal,
        "analysis_subject": subject,
        "intent_source": "server_rule",
        "intent_taxonomy_version": "v1",
        "metrics": metrics if metrics is not None else ["sessions"],
        "dimensions": dimensions if dimensions is not None else ["session_date"],
        "period_type": period_type,
        "requested_days": requested_days,
        "comparison_type": comparison,
        "resolution": resolution,
        "status": status,
        "latency_ms": latency,
        "result_row_count": 1,
        "error_code": error_code,
        "unsupported_reason": "external_source_not_available" if status == "unsupported" else None,
    }


class PreparationTests(unittest.TestCase):
    def test_deduplicates_by_contract_key_and_excludes_attachments_invalid_and_probe(self):
        first = event(1, "2026-09-07T01:00:00Z")
        duplicate = dict(first, latency_ms=999)
        attachment = dict(first, event_name="analytics_request_summary")
        malformed = dict(first, interaction_id="bad")
        probe = dict(event(2, "2026-09-07T02:00:00Z"), labels={"usage_validation": "true"})
        result = deduplicate_events([first, duplicate, attachment, malformed, probe])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["interaction_id"], first["interaction_id"])

    def test_raw_mapping_uses_the_full_wire_contract_and_drops_invalid_enum(self):
        valid = event(1, "2026-09-07T01:00:00Z")
        invalid = dict(event(2, "2026-09-07T02:00:00Z"), status="made_up_status")
        self.assertEqual(len(deduplicate_events([valid, invalid])), 1)

    def test_period_and_history_inputs_fail_closed(self):
        with self.assertRaises(KPIInputError):
            build_kpi_view([], "2026-09-08", "2026-09-07")
        with self.assertRaises(KPIInputError):
            build_kpi_view([], "2026/09/07", "2026-09-07")


class ActivationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = ActivationLedger(
            measurement_start="2026-08-01T00:00:00Z",
            measurement_version="pilot-v1",
            policy_approved=True,
        )

    def test_only_verified_success_analytics_create_idempotent_first_success(self):
        alice_late = event(1, "2026-08-10T01:00:00Z")
        alice_early = event(2, "2026-08-03T01:00:00Z")
        ignored_preflight = event(3, "2026-08-04T01:00:00Z", kind="capability_preflight")
        ignored_failure = event(4, "2026-08-05T01:00:00Z", status="failure")
        ignored_unknown = event(5, "2026-08-06T01:00:00Z", who=None, identity="unavailable")
        result = self.ledger.apply([alice_late, ignored_preflight, ignored_failure, ignored_unknown])
        self.assertEqual(result["inserted_users"], 1)
        self.assertEqual(self.ledger.snapshot()[0].first_success_at.isoformat(), "2026-08-10T01:00:00+00:00")
        result = self.ledger.apply([alice_late, alice_early])
        self.assertEqual(result["backdated_users"], 1)
        self.assertEqual(result["inserted_users"], 0)
        self.assertEqual(self.ledger.snapshot()[0].first_success_at.isoformat(), "2026-08-03T01:00:00+00:00")

    def test_calendar_year_retention_and_deletion_degrade_history(self):
        leap = ActivationLedger(measurement_start="2024-02-29T00:00:00Z", policy_approved=True)
        self.assertEqual(leap.retention_end.isoformat(), "2025-02-28T00:00:00+00:00")
        self.ledger.apply([event(1, "2026-08-03T01:00:00Z")])
        self.ledger.delete_user(user("alice"))
        coverage = self.ledger.history_coverage(event_history_start="2026-08-01", event_history_end="2026-09-01")
        result = coverage.evaluate(report_end=datetime(2026, 9, 1).date(), ledger_version="pilot-v1")
        self.assertFalse(result["can_publish_cumulative"])
        self.assertIn("ledger_deleted_or_expired", result["reasons"])

    def test_background_update_failure_is_isolated_and_marks_pipeline_gap(self):
        def broken_events():
            raise RuntimeError("source unavailable")
            yield  # pragma: no cover

        result = update_activation_ledger(self.ledger, broken_events())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "ledger_update_failed")
        self.assertTrue(self.ledger.pipeline_gap)


class KPIViewTests(unittest.TestCase):
    def setUp(self):
        self.alice = user("alice")
        self.bob = user("bob")
        self.ledger = ActivationLedger(
            measurement_start="2026-08-01T00:00:00Z",
            measurement_version="pilot-v1",
            policy_approved=True,
        )

    def complete_history(self):
        return HistoryCoverage(
            measurement_start=datetime(2026, 8, 1).date(),
            measurement_end=datetime(2027, 8, 1).date(),
            event_history_start=datetime(2026, 8, 1).date(),
            event_history_end=datetime(2026, 9, 30).date(),
            ledger_available=True,
            ledger_policy_approved=True,
            pipeline_complete=True,
        )

    def test_funnel_never_invents_external_denominators_and_excludes_invalid_mcp_calls(self):
        events = [
            event(1, "2026-09-07T01:00:00Z", who="alice"),
            event(2, "2026-09-07T02:00:00Z", who="bob", status="unsupported", resolution="unsupported", subject="cross_source"),
            event(3, "2026-09-07T03:00:00Z", who="bob", status="denied", error_code="tenant_access_denied", authorization="denied"),
            event(4, "2026-09-07T04:00:00Z", who="alice", status="failure", error_code="invalid_schema"),
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-07", as_of="2026-09-08T00:00:00Z")
        self.assertFalse(view["funnel"]["eligible"]["available"])
        self.assertEqual(view["funnel"]["eligible"]["reason"], "external_denominator_unavailable")
        self.assertFalse(view["funnel"]["authorized"]["available"])
        self.assertEqual(view["funnel"]["tried"]["count"], 2)
        self.assertEqual(view["funnel"]["activated"]["count"], 1)
        self.assertIsNone(view["funnel"]["conversion_rates"]["authorized_over_eligible"])

    def test_usage_grains_repeat_rate_and_sessions_are_distinct(self):
        events = [
            event(1, "2026-09-07T01:00:00Z", who="alice", latency=10),
            event(2, "2026-09-07T01:20:00Z", who="alice", latency=20),
            event(3, "2026-09-08T01:00:00Z", who="alice", transport="rest", tool="traffic_summary", latency=30),
            event(4, "2026-09-08T02:00:01Z", who="bob", latency=40),
            event(5, "2026-09-09T01:00:00Z", who="unknown", identity="unavailable", latency=50),
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-09", as_of="2026-09-10T00:00:00Z")
        usage = view["usage"]
        self.assertEqual(usage["analytics_requests"], 5)
        self.assertEqual(usage["successful_requests"], 5)
        self.assertEqual(usage["successful_users"], 2)
        self.assertEqual(usage["inferred_sessions"], 3)
        self.assertEqual(usage["active_days_per_user"]["distribution"], {"1": 1, "2": 1})
        self.assertEqual(usage["users_with_two_active_days"], 1)
        self.assertEqual(usage["repeat_usage_rate"], 0.5)
        self.assertEqual(usage["dau"]["series"], {"2026-09-07": 1, "2026-09-08": 2})

    def test_requests_per_user_keeps_failed_analytics_attempts(self):
        events = [
            event(1, "2026-09-07T01:00:00Z", who="alice", status="success"),
            event(2, "2026-09-07T02:00:00Z", who="alice", status="failure", error_code="timeout"),
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-07", as_of="2026-09-08T00:00:00Z")
        self.assertEqual(view["usage"]["analytics_requests_per_user"]["distribution"], {"2": 1})
        self.assertEqual(view["usage"]["active_days_per_user"]["distribution"], {"1": 1})

    def test_inferred_session_gap_includes_exactly_thirty_minutes(self):
        events = [
            event(1, "2026-09-07T01:00:00Z", who="alice"),
            event(2, "2026-09-07T01:30:00Z", who="alice"),
            event(3, "2026-09-07T02:00:01Z", who="alice"),
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-07", as_of="2026-09-08T00:00:00Z")
        self.assertEqual(view["usage"]["inferred_sessions"], 2)

    def test_funnel_uses_only_pseudonymous_external_denominator_ids(self):
        events = [event(1, "2026-09-07T01:00:00Z", who="alice")]
        view = build_kpi_view(
            events,
            "2026-09-07",
            "2026-09-07",
            eligible_users=[self.alice, "employee@example.com"],
            authorized_users=[self.alice],
            as_of="2026-09-08T00:00:00Z",
        )
        self.assertEqual(view["funnel"]["eligible"]["count"], 1)
        self.assertEqual(view["funnel"]["authorized"]["count"], 1)
        self.assertEqual(view["funnel"]["conversion_rates"]["authorized_over_eligible"], 1.0)

    def test_mixed_transport_funnel_does_not_publish_non_step_rate(self):
        view = build_kpi_view(
            [
                event(1, "2026-09-07T01:00:00Z", who="alice", transport="mcp"),
                event(2, "2026-09-07T02:00:00Z", who="bob", transport="rest"),
            ],
            "2026-09-07",
            "2026-09-07",
            as_of="2026-09-08T00:00:00Z",
        )
        funnel = view["funnel"]
        self.assertEqual(funnel["tried"]["count"], 1)
        self.assertEqual(funnel["activated"]["count"], 2)
        self.assertIsNone(funnel["conversion_rates"]["activated_over_tried"])

    def test_quality_and_demand_keep_preflight_separate_and_do_not_weight_rows(self):
        events = [
            event(1, "2026-09-07T01:00:00Z", kind="capability_preflight", subject="cross_source", status="unsupported", resolution="unsupported", metrics=["candidate"], dimensions=[]),
            event(2, "2026-09-07T02:00:00Z", metrics=["total_sessions", "total_sessions"], dimensions=["session_date", "session_date"], latency=200),
            event(3, "2026-09-07T03:00:00Z", who="bob", status="failure", error_code="timeout", resolution="supported", requested_days=90, comparison="none"),
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-07", as_of="2026-09-08T00:00:00Z")
        resolution = view["coverage"]["resolution"]
        self.assertEqual(resolution["capability_preflight"]["counts"]["unsupported"], 1)
        self.assertEqual(resolution["analytics"]["counts"]["supported"], 2)
        self.assertEqual(view["demand"]["metrics"], [{"value": "total_sessions", "count": 1}])
        self.assertEqual(view["demand"]["dimensions"], [{"value": "session_date", "count": 2}])
        self.assertEqual(view["demand"]["unsupported_reasons"]["capability_preflight"], [{"value": "external_source_not_available", "count": 1}])
        self.assertEqual(view["demand"]["unsupported_reasons"]["analytics"], [])
        self.assertIn({"request_kind": "analytics", "period_type": "explicit_range", "requested_days": 90, "comparison_type": "none", "count": 1}, view["demand"]["period_patterns"])

    def test_failure_and_denied_rates_use_analytics_only_denominator(self):
        events = [
            event(1, "2026-09-07T01:00:00Z", kind="capability_preflight", status="failure", resolution="needs_clarification"),
            event(2, "2026-09-07T02:00:00Z", status="success"),
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-07", as_of="2026-09-08T00:00:00Z")
        self.assertEqual(view["coverage"]["analytics_status_counts"]["failure"], 0)
        self.assertEqual(view["coverage"]["failure_rate"], 0.0)
        self.assertEqual(view["coverage"]["denied_rate"], 0.0)

    def test_traffic_demand_uses_the_report_contract_ids_only(self):
        metrics = ["total_sessions", "made_up_label"]
        dimensions = ["session_date", "made_up_dimension"]
        view = build_kpi_view(
            [event(1, "2026-09-07T01:00:00Z", metrics=metrics, dimensions=dimensions)],
            "2026-09-07",
            "2026-09-07",
            as_of="2026-09-08T00:00:00Z",
        )
        self.assertEqual(view["demand"]["metrics"], [{"value": "total_sessions", "count": 1}])
        self.assertEqual(view["demand"]["dimensions"], [{"value": "session_date", "count": 1}])

    def test_query_demand_uses_published_catalog_ids_only(self):
        events = [
            event(
                1,
                "2026-09-07T01:00:00Z",
                tool="query_ga4",
                metrics=["total_sessions", "made_up_metric"],
                dimensions=["event_date", "made_up_dimension"],
            )
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-07", as_of="2026-09-08T00:00:00Z")
        self.assertEqual(view["demand"]["metrics"], [{"value": "total_sessions", "count": 1}])
        self.assertEqual(view["demand"]["dimensions"], [{"value": "event_date", "count": 1}])

    def test_period_distribution_does_not_infer_or_merge_explicit_and_relative(self):
        events = [
            event(1, "2026-09-07T01:00:00Z", period_type="explicit_range", requested_days=7),
            event(2, "2026-09-07T02:00:00Z", period_type="relative_window", requested_days=7),
        ]
        view = build_kpi_view(events, "2026-09-07", "2026-09-07", as_of="2026-09-08T00:00:00Z")
        patterns = view["demand"]["period_patterns"]
        self.assertEqual(len(patterns), 2)
        self.assertEqual({item["period_type"] for item in patterns}, {"explicit_range", "relative_window"})

    def test_activation_and_w4_retention_require_complete_history(self):
        events = [
            event(1, "2026-08-03T01:00:00Z", who="alice"),  # W0
            event(2, "2026-08-31T01:00:00Z", who="alice"),  # W4
            event(3, "2026-08-10T01:00:00Z", who="bob"),   # W1 cohort
        ]
        self.ledger.apply(events)
        degraded = build_kpi_view(events, "2026-08-01", "2026-09-30", ledger=self.ledger, as_of="2026-09-30T00:00:00Z")
        self.assertEqual(degraded["activation"]["status"], "degraded")
        self.assertIsNone(degraded["activation"]["cumulative_users"])
        self.assertIsNone(degraded["activation"]["ledger_users"])
        self.assertEqual(degraded["retention"]["status"], "insufficient_history")
        available = build_kpi_view(
            events,
            "2026-08-01",
            "2026-09-30",
            ledger=self.ledger,
            history=self.complete_history(),
            as_of="2026-09-30T00:00:00Z",
        )
        self.assertEqual(available["activation"]["new_users"], 2)
        self.assertEqual(available["activation"]["cumulative_users"], 2)
        self.assertEqual(available["retention"]["status"], "available")
        self.assertEqual(available["retention"]["retained_users"], 1)
        # Bob's W0 also falls in the report window but has no W4 success, so
        # the mature cohort denominator is two users.
        self.assertEqual(available["retention"]["rate"], 0.5)

    def test_supplied_history_cannot_override_unapproved_ledger_policy(self):
        events = [event(1, "2026-08-03T01:00:00Z", who="alice")]
        unapproved = ActivationLedger(measurement_start="2026-08-01T00:00:00Z", policy_approved=False)
        unapproved.apply(events)
        view = build_kpi_view(
            events,
            "2026-08-01",
            "2026-08-31",
            ledger=unapproved,
            history=self.complete_history(),
            as_of="2026-09-01T00:00:00Z",
        )
        self.assertEqual(view["activation"]["status"], "degraded")
        self.assertIn("ledger_policy_unapproved", view["activation"]["history_coverage"]["reasons"])

    def test_weekly_summary_keeps_quality_and_history_metadata(self):
        events = [event(1, "2026-09-07T01:00:00Z")]
        summary = build_weekly_summary(events, "2026-09-09", as_of="2026-09-14T00:00:00Z")
        self.assertEqual(summary["report_type"], "usage_weekly_summary")
        self.assertEqual(summary["week"]["start_date"], "2026-09-07")
        self.assertEqual(summary["week"]["end_date"], "2026-09-13")
        self.assertIn("identity_coverage", summary["kpis"])
        self.assertEqual(summary["kpis"]["successful_requests"], 1)
        schema = json.loads((ROOT / "telemetry/weekly-summary.schema.v1.json").read_text())
        self.assertEqual(schema["properties"]["report_type"]["const"], "usage_weekly_summary")


class KPIPlanTests(unittest.TestCase):
    def test_offline_plan_has_ledger_view_boundaries_and_no_apply(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("plan_usage_kpis", ROOT / "scripts/plan_usage_kpis.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plan = module.plan()
        self.assertEqual(plan["mode"], "PLAN_ONLY")
        self.assertTrue(plan["controls"]["dashboard_reader_is_not_raw_event_reader"])
        self.assertEqual(plan["source"]["summary_dataset"], "not_joined")
        sql = module.render_sql()
        self.assertEqual(set(sql), {"usage-kpi-weekly.sql", "usage-kpi-quality.sql", "usage-kpi-demand.sql"})
        self.assertIn("CREATE OR REPLACE TABLE FUNCTION", sql["usage-kpi-weekly.sql"])
        self.assertIn("w4_retention_rate", sql["usage-kpi-weekly.sql"])
        self.assertIn("DATE_TRUNC", sql["usage-kpi-weekly.sql"])
        self.assertIn("history_complete", sql["usage-kpi-weekly.sql"])
        self.assertIn("measurement_version", sql["usage-kpi-weekly.sql"])
        self.assertIn("request_kind = 'analytics'", sql["usage-kpi-demand.sql"])
        self.assertIn("tool_name = 'query_ga4'", sql["usage-kpi-demand.sql"])
        with tempfile.TemporaryDirectory() as directory, patch("sys.argv", ["plan", "--output-dir", directory]):
            module.main()
            self.assertTrue((Path(directory) / "usage-kpi-plan.json").exists())


class ContractFixtureTests(unittest.TestCase):
    def test_schema_is_versioned_and_disallows_extra_top_level_fields(self):
        schema = json.loads((ROOT / "telemetry/kpi.schema.v1.json").read_text())
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.0")
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("activation", schema["required"])
        self.assertFalse(schema["$defs"]["coverage"]["additionalProperties"])
        self.assertIn("failure_rate", schema["$defs"]["coverage"]["required"])
        self.assertIn("history", schema["$defs"])
        weekly = json.loads((ROOT / "telemetry/weekly-summary.schema.v1.json").read_text())
        self.assertFalse(weekly["$defs"]["quality"]["additionalProperties"])
        self.assertIn("identity_coverage", weekly["$defs"]["kpis"]["required"])


if __name__ == "__main__":
    unittest.main()
