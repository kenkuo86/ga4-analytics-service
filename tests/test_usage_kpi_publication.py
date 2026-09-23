"""Executable publication decision table, shared by Python and rendered SQL.

DuckDB executes the SQLGlot-translated query body, not a rewritten KPI oracle.
Only source tables, function arguments, clock and Monday-week dialect syntax
are adapted. Native BigQuery dry-run/IAM remain deployment checks.
"""
from dataclasses import asdict, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any
import unittest

import duckdb
import sqlglot
from sqlglot import exp

from scripts.plan_usage_kpis import render_sql, plan
from tests.test_usage_kpis import event
from usage_kpis import ActivationLedger, HistoryCoverage, KPIInputError, build_kpi_view, update_activation_ledger


ZONE = ZoneInfo("Asia/Taipei")
BASE = HistoryCoverage(
    measurement_version="pilot-v1", measurement_start=date(2026, 8, 1),
    measurement_end=date(2027, 8, 1), event_history_start=date(2026, 8, 1),
    event_history_end=date(2026, 9, 30), ledger_available=True,
    ledger_policy_approved=True, identity_continuous=True, pipeline_complete=True,
)
# name, report end, observed through (inclusive full date), as_of, mature users, rate
CASES = (
    ("activation_without_followup", "2026-08-09", "2026-08-09", "2026-08-10T00:00:00+08:00", 0, None),
    ("sunday_start", "2026-08-09", "2026-09-06", "2026-09-06T00:00:00+08:00", 0, None),
    ("sunday_midday", "2026-08-09", "2026-09-06", "2026-09-06T12:00:00+08:00", 0, None),
    ("sunday_last_microsecond", "2026-08-09", "2026-09-06", "2026-09-06T23:59:59.999999+08:00", 0, None),
    ("monday_boundary", "2026-08-09", "2026-09-06", "2026-09-07T00:00:00+08:00", 1, 1.0),
    ("watermark_lags", "2026-08-09", "2026-09-05", "2026-09-07T00:00:00+08:00", 0, None),
    ("covered_subset", "2026-08-16", "2026-09-06", "2026-09-14T00:00:00+08:00", 1, 1.0),
    ("all_cohorts", "2026-08-16", "2026-09-13", "2026-09-14T00:00:00+08:00", 2, 0.5),
)
INVALID: tuple[Any, ...] = (None, "false", "true", 0, 1, "", [], {})


def ledger_for(events):
    ledger = ActivationLedger(measurement_start="2026-08-01T00:00:00+08:00",
                              measurement_version="pilot-v1", policy_approved=True,
                              identity_continuous=True)
    ledger.apply(events)
    return ledger


def execute_weekly(events, ledger, coverage, end, as_of):
    """Execute the entire generated weekly SELECT against synthetic typed tables."""
    body = render_sql()["usage-kpi-weekly.sql"].split("AS (", 1)[1].rsplit(");", 1)[0]
    query = sqlglot.parse_one(body, read="bigquery")
    sources = plan()
    table_names = {sources["ledger"]["table"]: "ledger", sources["measurement_metadata"]["table"]: "metadata"}
    for table in list(query.find_all(exp.Table)):
        if isinstance(table.this, exp.Anonymous):
            # Upstream canonical dedup is independently tested; fixtures are unique.
            table.set("this", exp.to_identifier("events"))
            table.set("db", None)
            table.set("catalog", None)
        else:
            full = ".".join(part.name for part in table.parts)
            if full in table_names:
                table.set("this", exp.to_identifier(table_names[full]))
                table.set("db", None)
                table.set("catalog", None)
    params = {"range_start": "2026-08-03", "range_end": end}
    for column in list(query.find_all(exp.Column)):
        if not column.table and column.name in params:
            value = exp.cast(exp.Literal.string(params[column.name]), "DATE")
            if isinstance(column.parent, exp.Select):
                value = exp.alias_(value, column.name)
            column.replace(value)
    for clock in list(query.find_all(exp.CurrentDate)):
        clock.replace(exp.cast(exp.Literal.string(datetime.fromisoformat(as_of).astimezone(ZONE).date().isoformat()), "DATE"))
    for trunc in query.find_all(exp.DateTrunc):
        # DuckDB's WEEK is ISO Monday; SQLGlot 28 retains BigQuery WEEK(MONDAY).
        if trunc.args["unit"].sql() == "WEEK(MONDAY)":
            trunc.set("unit", exp.Literal.string("week"))
    sql = query.sql(dialect="duckdb", unsupported_level=sqlglot.ErrorLevel.RAISE)
    with duckdb.connect(":memory:") as db:
        db.execute("SET TimeZone='UTC'")
        db.execute("CREATE TABLE events(user_id VARCHAR, event_time TIMESTAMPTZ, request_kind VARCHAR, status VARCHAR, identity_status VARCHAR)")
        for row in events:
            if datetime.fromisoformat(row["event_time"]) <= datetime.fromisoformat(as_of):
                db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)", [row[k] for k in ("user_id", "event_time", "request_kind", "status", "identity_status")])
        db.execute("CREATE TABLE ledger(user_id VARCHAR, first_success_at TIMESTAMPTZ, measurement_version VARCHAR)")
        for row in ledger.snapshot():
            db.execute("INSERT INTO ledger VALUES (?, ?, ?)", [row.user_id, row.first_success_at, row.measurement_version])
        types = sources["measurement_metadata"]["column_types"]
        db.execute("CREATE TABLE metadata(" + ", ".join(f"{k} {v}" for k, v in types.items()) + ")")
        values = {
            "measurement_version": coverage.measurement_version,
            "measurement_start": coverage.measurement_start, "measurement_end": coverage.measurement_end,
            "known_event_start": coverage.event_history_start, "known_event_end": coverage.event_history_end,
            "pipeline_watermark": as_of, "history_status": "degraded" if coverage.extra_reasons else "complete",
            "ledger_available": coverage.ledger_available, "ledger_policy_approved": coverage.ledger_policy_approved,
            "identity_continuous": coverage.identity_continuous, "pipeline_complete": coverage.pipeline_complete,
            "ledger_deleted_or_expired": coverage.ledger_deleted,
        }
        db.execute("INSERT INTO metadata VALUES (" + ",".join("?" for _ in types) + ")", [values[k] for k in types])
        result = db.execute(sql)
        return dict(zip([column[0] for column in result.description], result.fetchone()))


class PublicationContractTests(unittest.TestCase):
    def setUp(self):
        self.events = [event(1, "2026-08-03T01:00:00Z"),
                       event(2, "2026-08-10T01:00:00Z", who="bob"),
                       event(3, "2026-09-06T15:59:59Z")]

    def test_shared_temporal_decision_table(self):
        for name, end, watermark, as_of, mature, rate in CASES:
            with self.subTest(name=name):
                ledger = ledger_for(self.events)
                coverage = replace(BASE, event_history_end=date.fromisoformat(watermark))
                python = build_kpi_view(self.events, "2026-08-03", end, ledger=ledger, history=coverage, as_of=as_of)
                sql = execute_weekly(self.events, ledger, coverage, end, as_of)
                expected_users = 1 if end == "2026-08-09" else 2
                self.assertEqual(python["activation"]["new_users"], expected_users)
                self.assertEqual(sql["new_activated_users"], expected_users)
                self.assertEqual(python["retention"]["mature_users"], mature)
                self.assertEqual(sql["mature_w4_users"], mature)
                self.assertEqual(python["retention"]["rate"], rate)
                self.assertEqual(sql["w4_retention_rate"], rate)

    def test_measurement_end_is_exclusive_per_cohort(self):
        ledger = ActivationLedger(measurement_start="2026-08-01T00:00:00+08:00",
                                  retention_end="2026-09-07T00:00:00+08:00",
                                  measurement_version="pilot-v1", policy_approved=True,
                                  identity_continuous=True)
        ledger.apply(self.events)
        coverage = replace(BASE, measurement_end=date(2026, 9, 7))
        view = build_kpi_view(self.events, "2026-08-03", "2026-08-16", ledger=ledger,
                              history=coverage, as_of="2026-09-14T00:00:00Z")
        sql = execute_weekly(self.events, ledger, coverage, "2026-08-16", "2026-09-14T00:00:00Z")
        self.assertEqual(view["retention"]["mature_users"], 1)
        self.assertEqual(sql["mature_w4_users"], 1)
        self.assertEqual(view["retention"]["rate"], 1.0)
        self.assertEqual(sql["w4_retention_rate"], 1.0)

    def test_ledger_dates_follow_report_timezone_across_history_entry_points(self):
        ledger = ActivationLedger(
            measurement_start="2026-08-01T00:00:00+08:00",
            measurement_version="pilot-v1", policy_approved=True,
            identity_continuous=True,
        )
        ledger.apply(self.events)
        direct = ledger.history_coverage(
            event_history_start="2026-08-01", event_history_end="2026-09-30",
            pipeline_complete=True,
        )
        self.assertEqual(direct.measurement_start, date(2026, 8, 1))
        self.assertEqual(direct.event_history_start, date(2026, 8, 1))

        common = {
            "measurement_version": "pilot-v1",
            "measurement_start": date(2026, 8, 1),
            "measurement_end": date(2027, 8, 1),
            "event_history_start": date(2026, 8, 1),
            "event_history_end": date(2026, 9, 30),
            "ledger_available": True,
            "ledger_policy_approved": True,
            "identity_continuous": True,
            "pipeline_complete": True,
            "ledger_deleted": False,
        }
        inputs = (
            ("legacy", None),
            ("direct", direct),
            ("mapping", {**common, "measurement_start": "2026-08-01", "measurement_end": "2027-08-01",
                          "event_history_start": "2026-08-01", "event_history_end": "2026-09-30"}),
            ("object", HistoryCoverage(**common)),
            ("roundtrip", asdict(HistoryCoverage(**common))),
            ("offset_mapping", {**common,
                                 "measurement_start": "2026-08-01T00:00:00+08:00",
                                 "measurement_end": "2027-08-01T00:00:00+08:00",
                                 "event_history_start": "2026-08-01T00:00:00+08:00",
                                 "event_history_end": "2026-09-30T23:59:59+08:00"}),
        )
        for name, history in inputs:
            with self.subTest(path=name):
                kwargs = {} if history is None else {"history": history}
                view = build_kpi_view(
                    self.events, "2026-08-01", "2026-08-31", ledger=ledger,
                    event_history_start="2026-08-01", event_history_end="2026-09-30",
                    history_complete=True, as_of="2026-09-30T23:59:59+08:00",
                    **kwargs,
                )
                self.assertEqual(view["activation"]["status"], "available")
                self.assertEqual(view["activation"]["cumulative_users"], 2)
                self.assertEqual(view["retention"]["status"], "available")
                self.assertEqual(view["retention"]["mature_users"], 2)

        utc = ledger.history_coverage(
            event_history_start="2026-07-31T16:00:00Z",
            event_history_end="2026-09-30T16:00:00Z",
            pipeline_complete=True,
            timezone_name="UTC",
        )
        self.assertEqual(utc.measurement_start, date(2026, 7, 31))
        self.assertEqual(utc.event_history_start, date(2026, 7, 31))
        utc_view = build_kpi_view(
            self.events, "2026-08-01", "2026-08-31", ledger=ledger,
            history_complete=True,
            event_history_start="2026-07-31T16:00:00Z",
            event_history_end="2026-09-30T16:00:00Z",
            timezone_name="UTC", as_of="2026-10-01T00:00:00Z",
        )
        self.assertEqual(utc_view["activation"]["status"], "available")
        self.assertEqual(utc_view["activation"]["cumulative_users"], 2)

    def test_partial_history_publishes_only_fully_observed_cohorts(self):
        coverage = replace(BASE, event_history_end=date(2026, 9, 6))
        ledger = ledger_for(self.events)
        python = build_kpi_view(
            self.events, "2026-08-03", "2026-09-13", ledger=ledger,
            history=coverage, as_of="2026-09-14T00:00:00+08:00",
        )
        sql = execute_weekly(
            self.events, ledger, coverage, "2026-09-13", "2026-09-14T00:00:00+08:00",
        )
        self.assertEqual(python["activation"]["status"], "degraded")
        self.assertIsNone(python["activation"]["cumulative_users"])
        self.assertEqual(python["retention"]["status"], "available")
        self.assertEqual(python["retention"]["mature_users"], 1)
        self.assertEqual(python["retention"]["retained_users"], 1)
        self.assertEqual(python["retention"]["rate"], 1.0)
        self.assertEqual(sql["activation_status"], "degraded")
        self.assertIsNone(sql["cumulative_activated_users"])
        self.assertEqual(sql["mature_w4_users"], 1)
        self.assertEqual(sql["retained_w4_users"], 1)
        self.assertEqual(sql["w4_retention_rate"], 1.0)

    def test_all_evidence_fields_and_input_paths_fail_closed(self):
        for field in ("ledger_available", "ledger_policy_approved", "pipeline_complete", "identity_continuous", "ledger_deleted"):
            for value in INVALID:
                for path in ("mapping", "object", "roundtrip"):
                    with self.subTest(field=field, value=value, path=path):
                        raw = {**asdict(BASE), field: value}
                        supplied = raw if path == "mapping" else HistoryCoverage(**raw)
                        if path == "roundtrip":
                            supplied = asdict(supplied)
                        view = build_kpi_view(self.events, "2026-08-03", "2026-08-09",
                                              ledger=ledger_for(self.events), history=supplied,
                                              as_of="2026-09-14T00:00:00Z")
                        # None identity can be attested by the healthy ledger;
                        # invalid identity values may never be repaired by merge.
                        if field == "identity_continuous" and value is None:
                            self.assertEqual(view["activation"]["status"], "available")
                        else:
                            self.assertIsNone(view["activation"]["cumulative_users"])
                            self.assertIsNone(view["retention"]["rate"])

    def test_missing_identity_needs_an_attested_source(self):
        ledger = ledger_for(self.events)
        ledger.identity_continuous = None
        for supplied in ({k: v for k, v in asdict(BASE).items() if k != "identity_continuous"}, replace(BASE, identity_continuous=None)):
            view = build_kpi_view(self.events, "2026-08-03", "2026-08-09", ledger=ledger,
                                  history=supplied, as_of="2026-09-14T00:00:00Z")
            self.assertIsNone(view["activation"]["cumulative_users"])

    def test_negative_and_missing_evidence_sql_python(self):
        for field, value in (("ledger_available", False), ("ledger_policy_approved", False),
                             ("identity_continuous", False), ("pipeline_complete", False),
                             ("ledger_deleted", True), ("event_history_end", None)):
            with self.subTest(field=field):
                ledger = ledger_for(self.events)
                coverage = replace(BASE, **{field: value})
                python = build_kpi_view(self.events, "2026-08-03", "2026-08-09", ledger=ledger,
                                       history=coverage, as_of="2026-09-14T00:00:00Z")
                sql = execute_weekly(self.events, ledger, coverage, "2026-08-09", "2026-09-14T00:00:00Z")
                self.assertIsNone(python["activation"]["cumulative_users"])
                self.assertIsNone(sql["cumulative_activated_users"])
                self.assertIsNone(sql["w4_retention_rate"])

    def test_constructor_and_legacy_attestations(self):
        for value in INVALID:
            with self.subTest(value=value):
                ledger = ledger_for(self.events)
                coverage = ledger.history_coverage(event_history_start="2026-08-01", event_history_end="2026-09-30", pipeline_complete=value)
                self.assertFalse(coverage.evaluate(report_end=date(2026, 8, 9), ledger_version="pilot-v1")["can_publish_cumulative"])
                legacy = build_kpi_view(self.events, "2026-08-03", "2026-08-09", ledger=ledger,
                                        history_complete=value, event_history_start="2026-08-01",
                                        event_history_end="2026-09-30", as_of="2026-09-14T00:00:00Z")
                self.assertIsNone(legacy["activation"]["cumulative_users"])
                if value is not None:
                    with self.assertRaises(KPIInputError):
                        ActivationLedger(identity_continuous=value)
                    identity = ledger.history_coverage(event_history_start="2026-08-01", event_history_end="2026-09-30", pipeline_complete=True, identity_continuous=value)
                    self.assertFalse(identity.evaluate(report_end=date(2026, 8, 9), ledger_version="pilot-v1")["can_publish_cumulative"])
                unapproved = ActivationLedger(measurement_start="2026-08-01T00:00:00+08:00", measurement_version="pilot-v1", policy_approved=value, identity_continuous=True)
                self.assertFalse(BASE.with_ledger(unapproved, ZONE).evaluate(report_end=date(2026, 8, 9), ledger_version="pilot-v1")["can_publish_cumulative"])

    def test_version_and_negative_evidence_cannot_be_overridden(self):
        for field, value in (("measurement_version", None), ("measurement_version", "pilot-v2"),
                             ("identity_continuous", False), ("ledger_deleted", True), ("pipeline_complete", False)):
            for path in ("object", "mapping"):
                with self.subTest(field=field, path=path):
                    coverage = replace(BASE, **{field: value})
                    view = build_kpi_view(self.events, "2026-08-03", "2026-08-09", ledger=ledger_for(self.events),
                                          history=asdict(coverage) if path == "mapping" else coverage,
                                          as_of="2026-09-14T00:00:00Z")
                    self.assertIsNone(view["activation"]["cumulative_users"])
        for action in ("mark_pipeline_gap", "mark_identity_break", "expire"):
            ledger = ledger_for(self.events)
            getattr(ledger, action)()
            view = build_kpi_view(self.events, "2026-08-03", "2026-08-09", ledger=ledger,
                                  history=BASE, history_complete=True, as_of="2026-09-14T00:00:00Z")
            self.assertIsNone(view["activation"]["cumulative_users"])

    def test_rejected_batch_is_atomic_and_blocks_publication(self):
        for wrapper in (False, True):
            ledger = ledger_for([])
            bad = dict(self.events[0], interaction_id="invalid")
            if wrapper:
                self.assertEqual(update_activation_ledger(ledger, [self.events[0], bad])["status"], "failed")
            else:
                with self.assertRaises(KPIInputError):
                    ledger.apply([self.events[0], bad])
            self.assertEqual(ledger.snapshot(), ())
            self.assertTrue(ledger.pipeline_gap)
            view = build_kpi_view(self.events, "2026-08-03", "2026-08-09", ledger=ledger,
                                  history=BASE, as_of="2026-09-14T00:00:00Z")
            self.assertIsNone(view["activation"]["cumulative_users"])
        ledger = ledger_for([])
        probe = dict(self.events[0], labels={"usage_validation": "true"})
        self.assertEqual(update_activation_ledger(ledger, [self.events[0], self.events[0], probe])["status"], "ok")
        self.assertFalse(ledger.pipeline_gap)
        self.assertEqual(len(ledger.snapshot()), 1)
