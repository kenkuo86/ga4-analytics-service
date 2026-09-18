from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
import warnings

from pydantic import ValidationError

from traffic_summary_report import TRAFFIC_METRICS
from usage_contract import (
    SummaryAttachment, UsageEvent, bigquery_payload_schema, tenant_id_for_event,
)


def event(**changes):
    values = {
        "event_time": datetime(2026, 9, 18, 1, tzinfo=timezone.utc),
        "interaction_id": "8b812382-12f7-4cc6-b5fd-642e14764355",
        "transport": "mcp", "request_kind": "analytics", "status": "success",
        "latency_ms": 1350,
    }
    return UsageEvent(**(values | changes))


class UsageContractTests(unittest.TestCase):
    def test_example_and_checked_in_schemas_match_executable_contract(self):
        root = Path(__file__).resolve().parents[1] / "telemetry"
        record = UsageEvent.model_validate_json((root / "canonical-example.v1.json").read_text())
        self.assertEqual(record.metrics, [metric["metric_id"] for metric in TRAFFIC_METRICS])
        self.assertEqual(record.dimensions, ["session_date"])
        self.assertEqual(record.analysis_goal, "unknown")
        self.assertEqual(record.period_type, "explicit_range")
        self.assertEqual(record.requested_days, 7)
        self.assertIsNone(record.request_summary)
        for model, name in ((UsageEvent, "canonical"), (SummaryAttachment, "summary")):
            self.assertEqual(json.loads((root / f"{name}.schema.v1.json").read_text()), model.model_json_schema())
            self.assertEqual(json.loads((root / f"{name}.bigquery-payload.v1.json").read_text()), bigquery_payload_schema(model))

    def test_tenant_ids_preserve_opaque_contract(self):
        for value in ("5", "005", "tenant-a", " Tenant-A ", "", None):
            with self.subTest(value=value):
                self.assertEqual(event(tenant_id=value).wire_dict()["tenant_id"], value)
                self.assertEqual(tenant_id_for_event(value)[0], value)
        for value in (5, False, {}, ["5"]):
            with self.subTest(value=value):
                self.assertEqual(tenant_id_for_event(value), (None, "tenant_id_type_invalid"))
                with self.assertRaises(ValidationError):
                    event(tenant_id=value)
        self.assertEqual(tenant_id_for_event("x" * 257), (None, "tenant_id_too_long"))

    def test_canonical_rejects_text_and_entire_request_result(self):
        for key, value in (
            ("request_summary", "private text"), ("request_summary_source", "user_input"),
            ("Authorization", "Bearer secret"), ("cookie", "secret"),
            ("request", {"email": "employee@example.test"}), ("result", {"rows": [1]}),
            ("sql", "SELECT * FROM private"), ("error_code", "private exception"),
            ("host", "employee@example.test"), ("analysis_goal", "freeform"),
        ):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                event(**{key: value})

    def test_strict_numerics_and_bounds(self):
        for field in ("latency_ms", "requested_days", "result_row_count"):
            for invalid in (True, "7", 1.5, -1, 2**63):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValidationError):
                    event(**{field: invalid})
        with self.assertRaises(ValidationError):
            event(requested_days=0)
        self.assertEqual(event(result_row_count=0).result_row_count, 0)
        self.assertIsNone(event(status="denied").result_row_count)
        self.assertEqual(event(requested_days=1000, status="denied").requested_days, 1000)

    def test_identity_never_groups_unknown_users(self):
        self.assertIsNone(event().user_id)
        for changes in ({"identity_status": "verified"}, {"user_id": "a" * 64},
                        {"identity_status": "verified", "user_id": "person@example.test"}):
            with self.assertRaises(ValidationError):
                event(**changes)
        self.assertEqual(event(identity_status="verified", user_id="a" * 64).user_id, "a" * 64)

    def test_utc_timestamp_and_uuid(self):
        value = datetime(2026, 9, 18, 9, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(event(event_time=value).wire_dict()["event_time"], "2026-09-18T01:00:00Z")
        for changes in ({"event_time": datetime(2026, 9, 18)}, {"interaction_id": "client-id"}):
            with self.assertRaises(ValidationError):
                event(**changes)

    def test_array_bounds_deduplication_and_revalidation(self):
        self.assertEqual(event(metrics=["total_users", "total_users"]).metrics, ["total_users"])
        with self.assertRaises(ValidationError):
            event(metrics=["total_users"] * 101)
        record = event()
        record.metrics.append("private query text")
        with self.assertRaises(ValidationError):
            record.wire_dict()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(ValidationError):
                event().model_copy(update={"request_summary": "secret"}).wire_dict()
        self.assertEqual(caught, [])

    def test_summary_is_a_separate_bounded_attachment(self):
        base = {key: event().model_dump()[key] for key in ("event_time", "interaction_id")}
        for text in ("查" * 500, "😀" * 500):
            record = SummaryAttachment(**base, request_summary=text, request_summary_source="server_generated")
            self.assertEqual(len(record.request_summary), 500)
            self.assertEqual(set(record.wire_dict()), {
                "schema_version", "event_name", "event_time", "interaction_id",
                "request_summary", "request_summary_source",
            })
        for text in (None, "", "   ", "查" * 501):
            with self.assertRaises(ValidationError):
                SummaryAttachment(**base, request_summary=text, request_summary_source="user_input")
        with self.assertRaises(ValidationError):
            SummaryAttachment(**base, request_summary="safe", request_summary_source="unavailable")

    def test_payload_types_and_counting_units(self):
        schema = {field["name"]: field for field in bigquery_payload_schema(UsageEvent)}
        for name in ("tenant_id", "request_summary"):
            self.assertEqual(schema[name]["type"], "STRING")
            self.assertEqual(schema[name]["mode"], "NULLABLE")
        self.assertEqual(schema["requested_days"]["type"], "INT64")
        self.assertEqual(schema["metrics"]["mode"], "REPEATED")
        for kind in ("analytics", "capability_preflight", "discovery", "unclassified"):
            for status in ("success", "failure", "denied", "unsupported", "needs_clarification"):
                self.assertEqual(event(request_kind=kind, status=status).event_name, "analytics_request_completed")


if __name__ == "__main__":
    unittest.main()
