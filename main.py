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


class TenantResolutionError(ValueError):
    """A customer name could not be resolved to one active tenant."""

    def __init__(self, code: str, customer_name: str, message: str):
        super().__init__(message)
        self.code = code
        self.customer_name = customer_name
        self.message = message

    def as_result(self) -> dict:
        return {
            "status": self.code,
            "customer_name": self.customer_name,
            "message": self.message,
        }


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
    customer_name: str,
):
    """
    根據 registry 中的正式客戶名稱取得 GA4 BigQuery 的位置。
    """

    row, customer_name = get_tenant_record(client, customer_name)
    tenant_status = (row.status or "").strip().lower()

    if tenant_status != "active":
        raise TenantResolutionError(
            "tenant_inactive",
            customer_name,
            f"客戶「{row.tenant_name}」存在，但目前狀態為 {tenant_status or '未設定'}，尚未開放查詢。",
        )

    if not row.project_id:
        raise TenantResolutionError(
            "data_unavailable",
            customer_name,
            f"客戶「{row.tenant_name}」存在，但尚未設定 GA4 BigQuery 專案。",
        )

    # table identifier 無法使用 BigQuery query parameter，
    # 所以在放進 SQL 前先限制格式。
    identifier_pattern = r"^[A-Za-z0-9_\-]+$"

    if not re.match(identifier_pattern, row.project_id):
        raise ValueError("Invalid project_id")

    return {
        "tenant_id": row.tenant_id,
        "tenant_name": row.tenant_name,
        "project_id": row.project_id,
        "dataset_id": "ga4_mar",
    }


def get_tenant_record(client: bigquery.Client, customer_name: str):
    """Resolve an exact registered name without requiring analytics access."""

    customer_name = customer_name.strip()
    if not customer_name:
        raise TenantResolutionError(
            "invalid_customer_name",
            customer_name,
            "請提供客戶名稱。",
        )

    sql = f"""
    SELECT
      tenant_id,
      tenant_name,
      project_id,
      status
    FROM `{REGISTRY_TABLE}`
    WHERE NORMALIZE_AND_CASEFOLD(TRIM(tenant_name), NFKC)
      = NORMALIZE_AND_CASEFOLD(@customer_name, NFKC)
    LIMIT 2
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "customer_name",
                "STRING",
                customer_name,
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
        raise TenantResolutionError(
            "tenant_not_found",
            customer_name,
            f"tenant registry 中不存在客戶「{customer_name}」。",
        )

    if len(rows) > 1:
        raise TenantResolutionError(
            "ambiguous_tenant",
            customer_name,
            f"客戶名稱「{customer_name}」對應到多筆 tenant，請聯絡管理者修正 registry。",
        )

    return rows[0], customer_name


def get_customer_status(customer_name: str) -> dict:
    """Report registry existence independently from GA4 dataset availability."""

    client = get_bigquery_client()
    row, requested_name = get_tenant_record(client, customer_name)
    tenant_status = (row.status or "").strip().lower()
    analytics_available = tenant_status == "active" and bool(row.project_id)
    return {
        "status": "customer_found",
        "customer_name": row.tenant_name,
        "requested_name": requested_name,
        "tenant_status": tenant_status or "unset",
        "analytics_available": analytics_available,
        "data_source": (
            {
                "project_id": row.project_id,
                "dataset_id": "ga4_mar",
            }
            if row.project_id
            else None
        ),
        "message": f"客戶「{row.tenant_name}」存在於 tenant registry。",
    }


def get_available_customers() -> dict:
    """List uniquely named active customers with configured GA4 projects."""

    client = get_bigquery_client()
    sql = f"""
    WITH named_tenants AS (
      SELECT
        tenant_name,
        project_id,
        status,
        NORMALIZE_AND_CASEFOLD(TRIM(tenant_name), NFKC) AS normalized_name
      FROM `{REGISTRY_TABLE}`
      WHERE NULLIF(TRIM(tenant_name), '') IS NOT NULL
    ),
    uniquely_named AS (
      SELECT normalized_name
      FROM named_tenants
      GROUP BY normalized_name
      HAVING COUNT(*) = 1
    )
    SELECT tenant_name
    FROM named_tenants
    INNER JOIN uniquely_named USING (normalized_name)
    WHERE LOWER(TRIM(status)) = 'active'
      AND NULLIF(TRIM(project_id), '') IS NOT NULL
    ORDER BY normalized_name
    """

    rows = list(client.query(sql).result())
    customer_names = [row.tenant_name for row in rows]
    return {
        "status": "ok",
        "count": len(customer_names),
        "customers": customer_names,
        "availability_basis": (
            "active tenant with a non-empty, unique tenant_name and configured project_id"
        ),
    }


def get_traffic_summary(
    customer_name: str,
    start_date: str,
    end_date: str,
):
    client = get_bigquery_client()

    tenant = get_tenant_config(
        client=client,
        customer_name=customer_name,
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

    try:
        rows = client.query(
            sql,
            job_config=job_config,
        ).result()
    except Exception as error:
        raise TenantResolutionError(
            "data_unavailable",
            customer_name,
            f"客戶「{tenant['tenant_name']}」存在於 tenant registry，但目前無法取得 GA4 流量資料。",
        ) from error

    row = next(iter(rows))

    return {
        "status": "ok",
        "tenant": {
            "tenant_id": tenant["tenant_id"],
            "tenant_name": tenant["tenant_name"],
        },
        "data_source": {
            "project_id": tenant["project_id"],
            "dataset_id": tenant["dataset_id"],
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
    customer_name: str,
    start_date: str,
    end_date: str,
):
    try:
        return get_traffic_summary(
            customer_name=customer_name,
            start_date=start_date,
            end_date=end_date,
        )
    except TenantResolutionError as error:
        status_code = 404 if error.code == "tenant_not_found" else 409
        raise HTTPException(
            status_code=status_code,
            detail=error.as_result(),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


if __name__ == "__main__":
    result = get_traffic_summary(
        customer_name="維肯媒體部落格",
        start_date="2026-08-17",
        end_date="2026-08-23",
    )

    print(result)
