from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import google.auth  # noqa: E402
from google.auth import impersonated_credentials  # noqa: E402
from google.cloud import bigquery  # noqa: E402
from google.oauth2 import credentials as oauth2_credentials  # noqa: E402

from main import REGISTRY_TABLE  # noqa: E402
from semantic_catalog import semantic_catalog  # noqa: E402


DEFAULT_BILLING_PROJECT = "ga4-reports-dev"
DEFAULT_METRIC_IDS = ("total_users",)
GOOGLE_AUTH_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/drive",
)


def get_gcloud_source_credentials():
    """Use the active gcloud login without persisting its access token."""

    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        check=True,
        capture_output=True,
        text=True,
    )
    access_token = result.stdout.strip()
    if not access_token:
        raise RuntimeError("gcloud did not return an access token.")
    return oauth2_credentials.Credentials(token=access_token)


def get_credentials(
    target_service_account: str | None,
    *,
    use_gcloud_source_credentials: bool = False,
):
    if use_gcloud_source_credentials:
        source_credentials = get_gcloud_source_credentials()
    else:
        source_credentials, _ = google.auth.default(scopes=GOOGLE_AUTH_SCOPES)
    if not target_service_account:
        return source_credentials
    return impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=target_service_account,
        target_scopes=GOOGLE_AUTH_SCOPES,
        lifetime=3600,
    )


def get_active_tenants(
    client: bigquery.Client,
    registry_table: str,
) -> list[dict[str, Any]]:
    """Read all active tenant routes with exactly one real registry query."""

    sql = f"""
    SELECT
      tenant_id,
      tenant_name,
      TRIM(project_id) AS project_id,
      ec
    FROM `{registry_table}`
    WHERE LOWER(TRIM(status)) = 'active'
      AND NULLIF(TRIM(project_id), '') IS NOT NULL
    ORDER BY project_id, tenant_id
    """
    rows = client.query(sql).result()
    return [
        {
            "tenant_id": str(row.tenant_id),
            "tenant_name": row.tenant_name,
            "project_id": row.project_id,
            "dataset_id": "ga4_mar",
            "profile": "ecommerce" if row.ec is True else "non_ecommerce",
        }
        for row in rows
    ]


def published_metric_ids(profile: str) -> list[str]:
    metrics = semantic_catalog.profiles[profile]["metrics"]
    return sorted(
        metric_id
        for metric_id, metric in metrics.items()
        if metric["status"] == "published"
    )


def metric_ids_for_profile(
    profile: str,
    requested_metric_ids: Iterable[str],
    all_published_metrics: bool,
) -> list[str]:
    if all_published_metrics:
        return published_metric_ids(profile)
    return list(dict.fromkeys(requested_metric_ids))


def dry_run_metric(
    client: bigquery.Client,
    tenant: dict[str, Any],
    metric_id: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    sql, _ = semantic_catalog.compile_sql(
        profile=tenant["profile"],
        metric_id=metric_id,
        project_id=tenant["project_id"],
        dataset_id=tenant["dataset_id"],
        result_limit=1,
    )
    parameters = []
    if "@start_date" in sql:
        parameters.append(
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date)
        )
    if "@end_date" in sql:
        parameters.append(
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date)
        )
    job_config = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        query_parameters=parameters,
    )
    if job_config.dry_run is not True:
        raise RuntimeError("Tenant validation must never execute a real data query.")
    job = client.query(sql, job_config=job_config)
    return {
        "metric_id": metric_id,
        "status": "passed",
        "estimated_bytes_processed": int(job.total_bytes_processed or 0),
    }


def validate_tenants(
    client: bigquery.Client,
    tenants: Iterable[dict[str, Any]],
    *,
    requested_metric_ids: Iterable[str] = DEFAULT_METRIC_IDS,
    all_published_metrics: bool = False,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    tenant_results = []
    dry_run_count = 0
    dry_run_failure_count = 0
    estimated_bytes_processed = 0

    for tenant in tenants:
        metric_results = []
        metric_ids = metric_ids_for_profile(
            tenant["profile"],
            requested_metric_ids,
            all_published_metrics,
        )
        for metric_id in metric_ids:
            dry_run_count += 1
            try:
                result = dry_run_metric(
                    client,
                    tenant,
                    metric_id,
                    start_date,
                    end_date,
                )
                estimated_bytes_processed += result["estimated_bytes_processed"]
                metric_results.append(result)
            except Exception as error:
                dry_run_failure_count += 1
                metric_results.append(
                    {
                        "metric_id": metric_id,
                        "status": "failed",
                        "error": str(error),
                    }
                )

        tenant_results.append(
            {
                **tenant,
                "status": (
                    "passed"
                    if metric_results
                    and all(result["status"] == "passed" for result in metric_results)
                    else "failed"
                ),
                "metrics": metric_results,
            }
        )

    passed_tenant_count = sum(
        result["status"] == "passed" for result in tenant_results
    )
    return {
        "status": (
            "passed"
            if tenant_results and passed_tenant_count == len(tenant_results)
            else "failed"
        ),
        "catalog_version": semantic_catalog.version,
        "date_range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "cost_safety": {
            "registry_real_query_count": 1,
            "tenant_real_query_count": 0,
            "tenant_dry_run_count": dry_run_count,
            "dry_runs_are_billed": False,
            "estimated_bytes_processed_if_executed": estimated_bytes_processed,
        },
        "tenant_count": len(tenant_results),
        "passed_tenant_count": passed_tenant_count,
        "failed_tenant_count": len(tenant_results) - passed_tenant_count,
        "dry_run_failure_count": dry_run_failure_count,
        "tenants": tenant_results,
    }


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"日期必須使用 YYYY-MM-DD 格式：{value}"
        ) from error


def build_parser() -> argparse.ArgumentParser:
    today = date.today()
    parser = argparse.ArgumentParser(
        description=(
            "Query the active tenant registry once, then dry-run GA4 metrics for "
            "every tenant without executing tenant data queries."
        )
    )
    parser.add_argument(
        "--billing-project",
        default=os.getenv("BIGQUERY_BILLING_PROJECT", DEFAULT_BILLING_PROJECT),
    )
    parser.add_argument(
        "--registry-table",
        default=os.getenv("TENANT_REGISTRY_TABLE", REGISTRY_TABLE),
    )
    parser.add_argument(
        "--impersonate-service-account",
        default=os.getenv("RUNTIME_SERVICE_ACCOUNT"),
        help="Runtime service account email. Omit when already running as that SA.",
    )
    parser.add_argument(
        "--use-gcloud-source-credentials",
        action="store_true",
        help=(
            "Use the active gcloud login as the source for service-account "
            "impersonation instead of Application Default Credentials."
        ),
    )
    parser.add_argument(
        "--metric-id",
        action="append",
        dest="metric_ids",
        help="Metric to dry-run. Repeat for multiple metrics; default: total_users.",
    )
    parser.add_argument(
        "--all-published-metrics",
        action="store_true",
        help="Dry-run every published metric for each tenant profile.",
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=today - timedelta(days=7),
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=today - timedelta(days=1),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the JSON report; the report is always printed.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.start_date > args.end_date:
        raise SystemExit("start-date 不得晚於 end-date。")
    if args.all_published_metrics and args.metric_ids:
        raise SystemExit("--all-published-metrics 不可與 --metric-id 同時使用。")
    if args.use_gcloud_source_credentials and not args.impersonate_service_account:
        raise SystemExit(
            "--use-gcloud-source-credentials 必須搭配 "
            "--impersonate-service-account。"
        )

    credentials = get_credentials(
        args.impersonate_service_account,
        use_gcloud_source_credentials=args.use_gcloud_source_credentials,
    )
    client = bigquery.Client(
        credentials=credentials,
        project=args.billing_project,
    )
    tenants = get_active_tenants(client, args.registry_table)
    report = validate_tenants(
        client,
        tenants,
        requested_metric_ids=args.metric_ids or DEFAULT_METRIC_IDS,
        all_published_metrics=args.all_published_metrics,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    report["billing_project"] = args.billing_project
    report["registry_table"] = args.registry_table
    if args.use_gcloud_source_credentials:
        report["credential_mode"] = "gcloud_to_impersonated_runtime_service_account"
    elif args.impersonate_service_account:
        report["credential_mode"] = "adc_to_impersonated_runtime_service_account"
    else:
        report["credential_mode"] = "application_default_credentials"
    if args.impersonate_service_account:
        report["runtime_service_account"] = args.impersonate_service_account

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
