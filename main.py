from datetime import date
from pathlib import Path
import re

from google.cloud import bigquery
import google.auth

from fastapi import Depends, FastAPI, HTTPException

from oauth_auth import require_rest_oauth

app = FastAPI()

# 改成你實際存放 tenant_registry 的完整 table ID
REGISTRY_TABLE = "ora2-439609.ops.tenant_registry"


def get_bigquery_client():
    credentials, project = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/drive",
        ]
    )

    return bigquery.Client(
        credentials=credentials,
        project=project,
    )


def get_tenant_config(
    client: bigquery.Client,
    tenant_id: str,
):
    """
    根據 tenant_id 取得 GA4 BigQuery 的位置。
    """

    sql = f"""
    SELECT
      tenant_id,
      tenant_name,
      project_id,
      primary_dataset_id,
      status
    FROM `{REGISTRY_TABLE}`
    WHERE tenant_id = @tenant_id
    LIMIT 1
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "tenant_id",
                "STRING",
                tenant_id,
            )
        ]
    )

    rows = list(
        client.query(
            sql,
            job_config=job_config,
        ).result()
    )

    if not rows:
        raise ValueError(
            f"找不到 tenant_id: {tenant_id}"
        )

    row = rows[0]

    if row.status != "active":
        raise ValueError(
            f"tenant_id '{tenant_id}' 目前不是 active 狀態。"
        )

    if not row.project_id:
        raise ValueError(
            f"tenant_id '{tenant_id}' 沒有 project_id。"
        )

    if not row.primary_dataset_id:
        raise ValueError(
            f"tenant_id '{tenant_id}' 沒有 primary_dataset_id。"
        )

    # table identifier 無法使用 BigQuery query parameter，
    # 所以在放進 SQL 前先限制格式。
    identifier_pattern = r"^[A-Za-z0-9_\-]+$"

    if not re.match(identifier_pattern, row.project_id):
        raise ValueError("Invalid project_id")

    if not re.match(identifier_pattern, row.primary_dataset_id):
        raise ValueError("Invalid dataset_id")

    return {
        "tenant_id": row.tenant_id,
        "tenant_name": row.tenant_name,
        "project_id": row.project_id,
        "dataset_id": "ga4_mar",
    }


def get_traffic_summary(
    tenant_id: str,
    start_date: str,
    end_date: str,
):
    client = get_bigquery_client()

    tenant = get_tenant_config(
        client=client,
        tenant_id=tenant_id,
    )

    sql_path = (
        Path(__file__).parent
        / "queries"
        / "traffic_summary.sql"
    )

    sql_template = sql_path.read_text(
        encoding="utf-8"
    )

    sql = sql_template.format(
        project_id=tenant["project_id"],
        dataset_id=tenant["dataset_id"],
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "start_date",
                "DATE",
                date.fromisoformat(start_date),
            ),
            bigquery.ScalarQueryParameter(
                "end_date",
                "DATE",
                date.fromisoformat(end_date),
            ),
        ]
    )

    rows = client.query(
        sql,
        job_config=job_config,
    ).result()

    row = next(iter(rows))

    return {
        "tenant": {
            "tenant_id": tenant["tenant_id"],
            "tenant_name": tenant["tenant_name"],
        },
        "period": {
            "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat(),
        },
        "comparison_period": {
            "start_date": row.previous_start_date.isoformat(),
            "end_date": row.previous_end_date.isoformat(),
        },
        "current_period": {
            "total_sessions":
                row.current_period["total_sessions"],

            "total_users":
                row.current_period["total_users"],

            "new_users":
                row.current_period["new_users"],

            "returning_users":
                row.current_period["returning_users"],
        },
        "previous_period": {
            "total_sessions":
                row.previous_period["total_sessions"],

            "total_users":
                row.previous_period["total_users"],

            "new_users":
                row.previous_period["new_users"],

            "returning_users":
                row.previous_period["returning_users"],
        },
        "change_pct": {
            "total_sessions":
                row.change_pct["total_sessions"],

            "total_users":
                row.change_pct["total_users"],

            "new_users":
                row.change_pct["new_users"],

            "returning_users":
                row.change_pct["returning_users"],
        },
    }

@app.get(
    "/traffic-summary",
    dependencies=[Depends(require_rest_oauth)],
)
def traffic_summary(
    tenant_id: str,
    start_date: str,
    end_date: str,
):
    try:
        return get_traffic_summary(
            tenant_id=tenant_id,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


if __name__ == "__main__":
    result = get_traffic_summary(
        tenant_id="5",
        start_date="2026-08-17",
        end_date="2026-08-23",
    )

    print(result)
