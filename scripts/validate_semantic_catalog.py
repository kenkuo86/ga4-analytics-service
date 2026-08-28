from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google.cloud import bigquery  # noqa: E402

from main import get_bigquery_client, get_tenant_config  # noqa: E402
from semantic_catalog import SUPPORTED_PROFILES, semantic_catalog  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run every published semantic metric against a tenant schema."
    )
    parser.add_argument("--customer-name", required=True)
    parser.add_argument(
        "--profile",
        action="append",
        choices=SUPPORTED_PROFILES,
        dest="profiles",
    )
    args = parser.parse_args()

    client = get_bigquery_client()
    tenant = get_tenant_config(client, args.customer_name)
    profiles = args.profiles or list(SUPPORTED_PROFILES)
    successes = []
    failures = []
    for profile in profiles:
        metrics = semantic_catalog.profiles[profile]["metrics"]
        for metric_id, metric in metrics.items():
            if metric["status"] != "published":
                continue
            sql, _ = semantic_catalog.compile_sql(
                profile=profile,
                metric_id=metric_id,
                project_id=tenant["project_id"],
                dataset_id=tenant["dataset_id"],
                result_limit=1,
            )
            parameters = []
            if "@start_date" in sql:
                parameters.append(
                    bigquery.ScalarQueryParameter(
                        "start_date",
                        "DATE",
                        date(2026, 8, 17),
                    )
                )
            if "@end_date" in sql:
                parameters.append(
                    bigquery.ScalarQueryParameter(
                        "end_date",
                        "DATE",
                        date(2026, 8, 23),
                    )
                )
            job_config = bigquery.QueryJobConfig(
                dry_run=True,
                use_query_cache=False,
                query_parameters=parameters,
            )
            try:
                job = client.query(sql, job_config=job_config)
                successes.append(
                    {
                        "profile": profile,
                        "metric_id": metric_id,
                        "total_bytes_processed": job.total_bytes_processed,
                    }
                )
            except Exception as error:
                failures.append(
                    {
                        "profile": profile,
                        "metric_id": metric_id,
                        "error": str(error),
                    }
                )

    summary = {
        "catalog_version": semantic_catalog.version,
        "tenant_name": tenant["tenant_name"],
        "profiles": profiles,
        "success_count": len(successes),
        "failure_count": len(failures),
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
