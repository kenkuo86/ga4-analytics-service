#!/usr/bin/env python3
"""Render the Phase 11.6 KPI view plan without authenticating or mutating GCP.

The 11.5 routing plan owns the canonical deduplication table function.  This
script only renders bounded table-function SQL and a reviewable manifest for a
dashboard job.  It intentionally does not create datasets, tables, views or
IAM bindings.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from semantic_catalog import semantic_catalog
from traffic_summary_report import TRAFFIC_METRICS


PROJECT = "ga4-reports-dev"
LOCATION = "asia-east1"
EVENTS_DATASET = "ga4_mcp_test_events"
LEDGER_DATASET = "ga4_mcp_test_ledger"
LEDGER_TABLE = "activation_ledger_v1"
MEASUREMENT_TABLE = "measurement_metadata_v1"
KPI_SCHEMA_VERSION = "1.0"
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")


def _sql_literals(values: set[str] | tuple[str, ...] | list[str]) -> str:
    """Render trusted catalog IDs as quoted SQL literals."""
    return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def _quote(identifier: str) -> str:
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise ValueError("identifier contains an unsafe character")
    return f"`{identifier}`"


def plan(*, project: str = PROJECT, location: str = LOCATION) -> dict:
    """Return an offline manifest describing the 11.6 read-only layer."""
    _quote(project)
    _quote(location)
    events = f"{project}.{EVENTS_DATASET}.deduplicated_v1"
    ledger = f"{project}.{LEDGER_DATASET}.{LEDGER_TABLE}"
    metadata = f"{project}.{LEDGER_DATASET}.{MEASUREMENT_TABLE}"
    return {
        "schema_version": KPI_SCHEMA_VERSION,
        "component": "usage-kpi-dashboard",
        "mode": "PLAN_ONLY",
        "project": project,
        "location": location,
        "source": {
            "canonical_table_function": events,
            "event_identity": ["schema_version", "interaction_id", "event_name"],
            "event_name": "analytics_request_completed",
            "synthetic_marker_excluded": "labels.usage_validation != true",
            "summary_dataset": "not_joined",
        },
        "ledger": {
            "table": ledger,
            "dataset": LEDGER_DATASET,
            "columns": [
                {"name": "user_id", "type": "STRING", "mode": "REQUIRED"},
                {"name": "first_success_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
                {"name": "measurement_version", "type": "STRING", "mode": "REQUIRED"},
            ],
            "retention": "owner-approved measurement policy; never infer a permanent TTL",
            "stores": ["pseudonymous user_id", "first_success_at", "measurement_version"],
            "does_not_store": ["summary", "tenant_id", "metric_ids", "sql", "activity history"],
        },
        "measurement_metadata": {
            "table": metadata,
            "columns": [
                "measurement_version",
                "measurement_start",
                "measurement_end",
                "known_event_start",
                "known_event_end",
                "pipeline_watermark",
                "history_status",
                "ledger_available",
                "ledger_policy_approved",
                "identity_continuous",
                "pipeline_complete",
                "ledger_deleted_or_expired",
            ],
            "column_types": {
                "measurement_version": "STRING",
                "measurement_start": "DATE",
                "measurement_end": "DATE",
                "known_event_start": "DATE",
                "known_event_end": "DATE",
                "pipeline_watermark": "TIMESTAMP",
                "history_status": "STRING",
                "ledger_available": "BOOL",
                "ledger_policy_approved": "BOOL",
                "identity_continuous": "BOOL",
                "pipeline_complete": "BOOL",
                "ledger_deleted_or_expired": "BOOL",
            },
            "purpose": "make history coverage and downgrade state explicit",
        },
        "datasets": [
            {
                "dataset": LEDGER_DATASET,
                "location": location,
                "default_expiration": "must be explicitly approved; do not inherit the 180-day event TTL",
            }
        ],
        "views": [
            {"name": "usage_kpi_weekly_v1", "grain": "week", "source": "canonical + ledger + metadata", "max_range_days": 146},
            {"name": "usage_kpi_quality_v1", "grain": "request_kind/status/resolution", "source": "canonical"},
            {"name": "usage_kpi_demand_v1", "grain": "validated metric/intent IDs", "source": "canonical"},
        ],
        "controls": {
            "timezone": "Asia/Taipei",
            "week_start": "monday",
            "inferred_session_gap_minutes": 30,
            "canonical_retention_days": 180,
            "summary_retention_days": 30,
            "require_partition_filter": True,
            "maximum_bytes_billed_initial": 100000000,
            "dashboard_reader_is_not_raw_event_reader": True,
            "eligible_and_authorized_denominators": "external tables required; never infer from canonical events",
            "unapproved_ledger": "degrade cumulative activation and cohort metrics",
        },
        "preconditions": [
            "Approve the concrete activation-ledger retention and deletion policy before publishing cumulative KPIs.",
            "Grant dashboard readers only the aggregate view permissions; do not grant raw event or summary access.",
            "Verify the measurement metadata watermark and identity-key continuity for each measurement version.",
            "Dry-run every table function with a bounded date range and maximum_bytes_billed before deployment.",
            "Do not join the 30-day summary attachment into a long-lived KPI view.",
            "Keep weekly input ranges at most 146 inclusive local dates so the W4 source extension stays within the 180-day event window.",
        ],
        "deployment": {"apply": False, "dashboard_refresh": False},
    }


def render_sql(*, project: str = PROJECT) -> dict[str, str]:
    """Render parameterized table functions for a reviewable SQL handoff."""
    _quote(project)
    source = _quote(f"{project}.{EVENTS_DATASET}.deduplicated_v1")
    ledger = _quote(f"{project}.{LEDGER_DATASET}.{LEDGER_TABLE}")
    metadata = _quote(f"{project}.{LEDGER_DATASET}.{MEASUREMENT_TABLE}")
    traffic_metric_ids = {metric["metric_id"] for metric in TRAFFIC_METRICS}
    traffic_dimension_ids = {"session_date"}
    catalog_metric_ids = {
        metric_id
        for profile in semantic_catalog.profiles.values()
        for metric_id, metric in profile["metrics"].items()
        if metric.get("status") == "published"
    }
    catalog_dimension_ids = set(semantic_catalog.dimensions)
    traffic_metric_sql = _sql_literals(traffic_metric_ids)
    traffic_dimension_sql = _sql_literals(traffic_dimension_ids)
    catalog_metric_sql = _sql_literals(catalog_metric_ids)
    catalog_dimension_sql = _sql_literals(catalog_dimension_ids)
    weekly = f"""-- Phase 11.6 logical view; execute only after the 11.5 source is verified.
-- The source table function already deduplicates by schema_version, interaction_id, event_name.
-- A weekly input may span at most 146 inclusive local dates; the bounded
-- source end below reserves 34 follow-up days inside the 180-day event window.
CREATE OR REPLACE TABLE FUNCTION {_quote(f'{project}.{EVENTS_DATASET}.usage_kpi_weekly_v1')}(range_start DATE, range_end DATE)
AS (
  WITH parameters AS (
    SELECT
      range_start,
      range_end,
      DATE_DIFF(range_end, range_start, DAY) BETWEEN 0 AND 145 AS range_supported,
      LEAST(
        GREATEST(range_end, range_start),
        DATE_ADD(range_start, INTERVAL 145 DAY)
      ) AS bounded_end
  ),
  canonical AS (
    SELECT source_event.*, parameters.range_supported
    FROM {source}(
      range_start,
      DATE_ADD(
        LEAST(GREATEST(range_end, range_start), DATE_ADD(range_start, INTERVAL 145 DAY)),
        INTERVAL 34 DAY
      )
    ) AS source_event
    CROSS JOIN parameters
    WHERE source_event.event_time >= TIMESTAMP(parameters.range_start, 'Asia/Taipei')
      AND source_event.event_time < TIMESTAMP(DATE_ADD(parameters.bounded_end, INTERVAL 35 DAY), 'Asia/Taipei')
  ),
  period_success AS (
    SELECT user_id, DATE(event_time, 'Asia/Taipei') AS local_date
    FROM canonical
    CROSS JOIN parameters
    WHERE request_kind = 'analytics' AND status = 'success'
      AND identity_status = 'verified' AND user_id IS NOT NULL
      AND DATE(event_time, 'Asia/Taipei') BETWEEN parameters.range_start AND parameters.range_end
  ),
  follow_up_success AS (
    SELECT user_id, DATE(event_time, 'Asia/Taipei') AS local_date
    FROM canonical
    WHERE request_kind = 'analytics' AND status = 'success'
      AND identity_status = 'verified' AND user_id IS NOT NULL
  ),
  metadata_latest AS (
    SELECT *
    FROM {metadata}
    QUALIFY ROW_NUMBER() OVER (ORDER BY pipeline_watermark DESC) = 1
  ),
  metadata_evidence AS (
    -- Aggregate without GROUP BY deliberately returns one degraded row when
    -- metadata is empty; it must never suppress the observable period counts.
    SELECT
      ANY_VALUE(parameters.range_supported) AS range_supported,
      ANY_VALUE(parameters.range_start) AS range_start,
      ANY_VALUE(parameters.range_end) AS range_end,
      COALESCE(MAX(metadata_latest.history_status), 'degraded') AS source_history_status,
      MAX(metadata_latest.measurement_version) AS measurement_version,
      MAX(metadata_latest.known_event_end) AS known_event_end,
      MAX(metadata_latest.measurement_end) AS measurement_end,
      COALESCE(LOGICAL_AND(metadata_latest.ledger_available), FALSE)
        AND COALESCE(LOGICAL_AND(metadata_latest.ledger_policy_approved), FALSE)
        AND COALESCE(LOGICAL_AND(metadata_latest.identity_continuous), FALSE)
        AND COALESCE(LOGICAL_AND(metadata_latest.pipeline_complete), FALSE)
        AND NOT COALESCE(LOGICAL_OR(metadata_latest.ledger_deleted_or_expired), TRUE)
        AND COALESCE(MAX(metadata_latest.known_event_start) <= ANY_VALUE(parameters.range_start), FALSE)
        AND COALESCE(MAX(metadata_latest.known_event_start) <= MAX(metadata_latest.measurement_start), FALSE)
        AND COALESCE(MAX(metadata_latest.measurement_end) > ANY_VALUE(parameters.range_end), FALSE)
        AND COALESCE(MAX(metadata_latest.history_status) = 'complete', FALSE)
        AND ANY_VALUE(parameters.range_supported) AS ledger_history_complete
    FROM parameters
    LEFT JOIN metadata_latest ON TRUE
  ),
  metadata_gate AS (
    SELECT
      metadata_evidence.*,
      metadata_evidence.ledger_history_complete
        AND COALESCE(metadata_evidence.known_event_end >= metadata_evidence.range_end, FALSE)
        AS activation_history_complete,
      metadata_evidence.ledger_history_complete
        AND COALESCE(metadata_evidence.known_event_end >= metadata_evidence.range_end, FALSE)
        AS retention_history_complete
    FROM metadata_evidence
  ),
  scoped_ledger AS (
    SELECT ledger_row.*
    FROM {ledger} AS ledger_row
    CROSS JOIN metadata_gate
    CROSS JOIN parameters
    WHERE metadata_gate.ledger_history_complete
      AND ledger_row.measurement_version = metadata_gate.measurement_version
      AND DATE(ledger_row.first_success_at, 'Asia/Taipei') <= parameters.range_end
  ),
  cohorts AS (
    SELECT
      user_id,
      DATE_TRUNC(DATE(first_success_at, 'Asia/Taipei'), WEEK(MONDAY)) AS w0
    FROM scoped_ledger
    CROSS JOIN parameters
    WHERE DATE_TRUNC(DATE(first_success_at, 'Asia/Taipei'), WEEK(MONDAY)) BETWEEN parameters.range_start AND parameters.range_end
  ),
  mature_cohorts AS (
    -- Maturity is per cohort and follows the positively attested event-history
    -- boundary.  It is independent of the requested cohort-selection end.
    SELECT cohorts.*
    FROM cohorts
    CROSS JOIN metadata_gate
    WHERE metadata_gate.retention_history_complete
      AND DATE_ADD(cohorts.w0, INTERVAL 34 DAY) <= metadata_gate.known_event_end
      AND DATE_ADD(cohorts.w0, INTERVAL 34 DAY) < metadata_gate.measurement_end
  ),
  retained_cohorts AS (
    SELECT DISTINCT cohort.user_id
    FROM mature_cohorts AS cohort
    JOIN follow_up_success AS follow_up
      ON follow_up.user_id = cohort.user_id
     AND follow_up.local_date BETWEEN DATE_ADD(cohort.w0, INTERVAL 28 DAY)
                                  AND DATE_ADD(cohort.w0, INTERVAL 34 DAY)
  ),
  active AS (
    SELECT
      COUNT(DISTINCT user_id) AS successful_users,
      COUNT(*) AS successful_requests,
      COUNT(DISTINCT FORMAT_DATE('%F', local_date) || ':' || user_id) AS user_day_pairs
    FROM period_success
  ),
  ledger_counts AS (
    SELECT
      COUNT(*) AS ledger_users,
      COUNTIF(DATE(first_success_at, 'Asia/Taipei') BETWEEN parameters.range_start AND parameters.range_end) AS new_users,
      COUNTIF(DATE(first_success_at, 'Asia/Taipei') <= parameters.range_end) AS cumulative_users
    FROM scoped_ledger
    CROSS JOIN parameters
  )
  SELECT
    parameters.range_start AS period_start,
    parameters.range_end AS period_end,
    'Asia/Taipei' AS timezone,
    active.successful_users,
    active.successful_requests,
    active.user_day_pairs,
    CASE WHEN metadata_gate.range_supported THEN active.successful_users ELSE NULL END AS observed_period_users,
    CASE WHEN metadata_gate.activation_history_complete THEN 'available' ELSE 'degraded' END AS activation_status,
    CASE WHEN metadata_gate.activation_history_complete THEN ledger_counts.new_users ELSE NULL END AS new_activated_users,
    CASE WHEN metadata_gate.activation_history_complete THEN ledger_counts.cumulative_users ELSE NULL END AS cumulative_activated_users,
    CASE WHEN metadata_gate.activation_history_complete THEN ledger_counts.ledger_users ELSE NULL END AS ledger_users,
    CASE WHEN metadata_gate.retention_history_complete THEN (SELECT COUNT(*) FROM mature_cohorts) ELSE NULL END AS mature_w4_users,
    CASE WHEN metadata_gate.retention_history_complete THEN (SELECT COUNT(*) FROM retained_cohorts) ELSE NULL END AS retained_w4_users,
    CASE WHEN metadata_gate.retention_history_complete THEN SAFE_DIVIDE((SELECT COUNT(*) FROM retained_cohorts), (SELECT COUNT(*) FROM mature_cohorts)) ELSE NULL END AS w4_retention_rate,
    CASE WHEN metadata_gate.range_supported THEN metadata_gate.source_history_status ELSE 'degraded' END AS history_status,
    metadata_gate.measurement_version
  FROM active
  CROSS JOIN ledger_counts
  CROSS JOIN metadata_gate
  CROSS JOIN parameters
);
"""
    quality = f"""-- Quality and outcome view. Rates keep capability_preflight and analytics denominators separate.
CREATE OR REPLACE TABLE FUNCTION {_quote(f'{project}.{EVENTS_DATASET}.usage_kpi_quality_v1')}(range_start DATE, range_end DATE)
AS (
  SELECT request_kind, status, resolution, tool_name, transport, error_code, COUNT(*) AS event_count,
         APPROX_QUANTILES(latency_ms, 100)[OFFSET(50)] AS p50_latency_ms,
         APPROX_QUANTILES(latency_ms, 100)[OFFSET(95)] AS p95_latency_ms
  FROM {source}(range_start, range_end)
  GROUP BY request_kind, status, resolution, tool_name, transport, error_code
);
"""
    demand = f"""-- Demand view. Each array is unnested separately so metric and dimension counts
-- are not multiplied by a cross-product and are never weighted by result rows.
-- IDs are accepted only from the versioned report contract or semantic catalog;
-- preflight candidates and arbitrary labels are excluded by request_kind/tool.
CREATE OR REPLACE TABLE FUNCTION {_quote(f'{project}.{EVENTS_DATASET}.usage_kpi_demand_v1')}(range_start DATE, range_end DATE)
AS (
  SELECT request_kind, analysis_goal, analysis_subject, intent_source, period_type, requested_days,
         comparison_type, metric_id, CAST(NULL AS STRING) AS dimension_id, COUNT(*) AS request_count
  FROM (
    SELECT DISTINCT interaction_id, request_kind, analysis_goal, analysis_subject,
           intent_source, period_type, requested_days, comparison_type, metric_id
    FROM {source}(range_start, range_end), UNNEST(metrics) AS metric_id
    WHERE request_kind = 'analytics'
      AND (
        (tool_name = 'traffic_summary' AND metric_id IN ({traffic_metric_sql}))
        OR (tool_name = 'query_ga4' AND metric_id IN ({catalog_metric_sql}))
      )
  )
  GROUP BY request_kind, analysis_goal, analysis_subject, intent_source, period_type,
           requested_days, comparison_type, metric_id
  UNION ALL
  SELECT request_kind, analysis_goal, analysis_subject, intent_source, period_type, requested_days,
         comparison_type, CAST(NULL AS STRING) AS metric_id, dimension_id, COUNT(*) AS request_count
  FROM (
    SELECT DISTINCT interaction_id, request_kind, analysis_goal, analysis_subject,
           intent_source, period_type, requested_days, comparison_type, dimension_id
    FROM {source}(range_start, range_end), UNNEST(dimensions) AS dimension_id
    WHERE request_kind = 'analytics'
      AND (
        (tool_name = 'traffic_summary' AND dimension_id IN ({traffic_dimension_sql}))
        OR (tool_name = 'query_ga4' AND dimension_id IN ({catalog_dimension_sql}))
      )
  )
  GROUP BY request_kind, analysis_goal, analysis_subject, intent_source, period_type,
           requested_days, comparison_type, dimension_id
);
"""
    return {"usage-kpi-weekly.sql": weekly, "usage-kpi-quality.sql": quality, "usage-kpi-demand.sql": demand}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--location", default=LOCATION)
    args = parser.parse_args()
    document = plan(project=args.project, location=args.location)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "usage-kpi-plan.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    for name, sql in render_sql(project=args.project).items():
        (args.output_dir / name).write_text(sql)
    print("Plan only: wrote KPI manifest and SQL; no cloud authentication or mutation.")


if __name__ == "__main__":
    main()
