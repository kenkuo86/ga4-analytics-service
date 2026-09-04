from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import re
from typing import Any

from google.cloud import bigquery
import google.auth

from fastapi import Depends, FastAPI, HTTPException

from oauth_auth import require_rest_oauth
from query_policy import PreparedQuery, QueryPolicyError, query_policy
from semantic_catalog import SemanticCatalogError, semantic_catalog

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
    credentials, detected_project = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/drive",
        ]
    )
    billing_project = (
        os.getenv("BIGQUERY_BILLING_PROJECT", "").strip()
        or detected_project
    )

    return bigquery.Client(
        credentials=credentials,
        project=billing_project,
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
        # Registry policy: only literal TRUE means ecommerce. FALSE and blank
        # retain the non-ecommerce behavior used before the column existed.
        "semantic_profile": "ecommerce" if row.ec is True else "non_ecommerce",
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
      status,
      ec
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

    try:
        rows = list(
            client.query(
                sql,
                job_config=job_config,
            ).result()
        )
    except Exception as error:
        mapped_error = query_policy.map_bigquery_error(error)
        if mapped_error is not None:
            raise mapped_error from error
        raise

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
        "semantic_profile": "ecommerce" if row.ec is True else "non_ecommerce",
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

    try:
        rows = list(client.query(sql).result())
    except Exception as error:
        mapped_error = query_policy.map_bigquery_error(error)
        if mapped_error is not None:
            raise mapped_error from error
        raise
    customer_names = [row.tenant_name for row in rows]
    return {
        "status": "ok",
        "count": len(customer_names),
        "customers": customer_names,
        "availability_basis": (
            "active tenant with a non-empty, unique tenant_name and configured project_id"
        ),
    }


def search_ga4_metric_catalog(
    query: str,
    profile: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search the published semantic metric catalog without querying tenant data."""

    return semantic_catalog.search(
        query=query,
        profile=profile,
        limit=limit,
    )


def _serialize_bigquery_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _serialize_bigquery_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serialize_bigquery_value(item) for item in value]
    if hasattr(value, "items"):
        return {
            str(key): _serialize_bigquery_value(item)
            for key, item in value.items()
        }
    return str(value)


def query_ga4_semantic_metrics(
    customer_name: str,
    metric_ids: list[str],
    start_date: str,
    end_date: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Execute catalog-approved metric SQL for one resolved tenant."""

    if not isinstance(metric_ids, list) or not metric_ids:
        raise SemanticCatalogError(
            "invalid_metric_request",
            "請至少提供一個 metric_id。",
        )
    if len(metric_ids) > 5:
        raise SemanticCatalogError(
            "too_many_metrics",
            "單次最多查詢 5 個指標。",
        )
    normalized_metric_ids = []
    for metric_id in metric_ids:
        if not isinstance(metric_id, str):
            raise SemanticCatalogError(
                "invalid_metric_request",
                "每個 metric_id 都必須是字串。",
            )
        normalized = metric_id.strip()
        if not normalized or not re.fullmatch(r"[a-z0-9_]+", normalized):
            raise SemanticCatalogError(
                "invalid_metric_request",
                f"metric_id「{metric_id}」格式不合法。",
            )
        if normalized not in normalized_metric_ids:
            normalized_metric_ids.append(normalized)

    parsed_start, parsed_end = query_policy.validate_date_range(start_date, end_date)
    result_limit = max(1, min(int(limit), 200))
    client = get_bigquery_client()
    tenant = get_tenant_config(client, customer_name)
    resolved_profile, _ = semantic_catalog.resolve_profile(
        normalized_metric_ids,
        tenant["semantic_profile"],
    )
    profile_resolution = "tenant_registry.ec"
    prepared_metrics = []
    for metric_id in normalized_metric_ids:
        sql, metric = semantic_catalog.compile_sql(
            profile=resolved_profile,
            metric_id=metric_id,
            project_id=tenant["project_id"],
            dataset_id=tenant["dataset_id"],
            result_limit=result_limit,
        )
        date_scope = (
            "requested_period"
            if "@start_date" in sql and "@end_date" in sql
            else "all_available_data"
        )
        query_parameters = []
        if "@start_date" in sql:
            query_parameters.append(
                bigquery.ScalarQueryParameter("start_date", "DATE", parsed_start)
            )
        if "@end_date" in sql:
            query_parameters.append(
                bigquery.ScalarQueryParameter("end_date", "DATE", parsed_end)
            )
        prepared_metrics.append(
            {
                "metric_id": metric_id,
                "metric": metric,
                "date_scope": date_scope,
                "query": PreparedQuery(
                    name=metric_id,
                    sql=sql,
                    query_parameters=query_parameters,
                    labels={
                        "component": "semantic",
                        "profile": resolved_profile.replace("_", "-")[:63],
                    },
                ),
            }
        )

    query_policy.preflight_request(
        client,
        [item["query"] for item in prepared_metrics],
    )

    metric_results = []
    for item in prepared_metrics:
        metric_id = item["metric_id"]
        metric = item["metric"]
        try:
            _, rows = query_policy.execute(client, item["query"])
            rows = list(rows)
        except QueryPolicyError:
            raise
        except Exception as error:
            raise SemanticCatalogError(
                "data_unavailable",
                f"客戶「{tenant['tenant_name']}」的指標「{metric_id}」目前無法查詢。",
                details={"metric_id": metric_id},
            ) from error

        truncated = len(rows) > result_limit
        serialized_rows = [
            _serialize_bigquery_value(dict(row.items()))
            for row in rows[:result_limit]
        ]
        metric_results.append(
            {
                "metric_id": metric_id,
                "label": metric["label"],
                "main_metric": metric["main_metric"],
                "category": metric["category"],
                "dimensions": metric["dimensions"],
                "date_scope": item["date_scope"],
                "row_count": len(serialized_rows),
                "truncated": truncated,
                "rows": serialized_rows,
            }
        )

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
        "semantic": {
            "catalog_version": semantic_catalog.version,
            "profile": resolved_profile,
            "profile_resolution": profile_resolution,
        },
        "period": {
            "start_date": parsed_start.isoformat(),
            "end_date": parsed_end.isoformat(),
        },
        "metrics": metric_results,
    }


def get_traffic_summary(
    customer_name: str,
    start_date: str,
    end_date: str,
):
    parsed_start, parsed_end = query_policy.validate_date_range(
        start_date,
        end_date,
        comparison_periods=1,
    )
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

    prepared_query = PreparedQuery(
        name="traffic_summary",
        sql=sql,
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "start_date",
                "DATE",
                parsed_start,
            ),
            bigquery.ScalarQueryParameter(
                "end_date",
                "DATE",
                parsed_end,
            ),
        ],
        labels={"component": "traffic-summary"},
    )

    query_policy.preflight_request(client, [prepared_query])

    try:
        _, rows = query_policy.execute(client, prepared_query)
    except QueryPolicyError:
        raise
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
    except QueryPolicyError as error:
        status_code = {
            "daily_query_quota_exceeded": 429,
            "query_cost_estimate_failed": 503,
            "query_timeout": 504,
        }.get(error.code, 400)
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
