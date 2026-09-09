from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import re
from typing import Any

from google.cloud import bigquery
import google.auth

from fastapi import Depends, FastAPI, HTTPException

from capability_registry import capability_registry
from oauth_auth import require_rest_oauth
from query_policy import (
    QUERY_PROVENANCE_SCHEMA_VERSION,
    PreparedQuery,
    QueryPolicyError,
    attach_query_provenance,
    build_query_provenance,
    query_policy,
)
from semantic_catalog import SemanticCatalogError, semantic_catalog
from tenant_context import TenantContextErrorMixin, TenantRequestContext
from tenant_registry import (
    ALIAS_SEPARATOR,
    is_broad_customer_search,
    normalize_customer_name,
)
from traffic_summary_report import (
    TrafficSummaryReportError,
    build_traffic_summary_report,
)

app = FastAPI()

# 改成你實際存放 tenant_registry 的完整 table ID
REGISTRY_TABLE = "ora2-439609.ops.tenant_registry"


class TenantResolutionError(TenantContextErrorMixin, ValueError):
    """A customer name could not be resolved to one active tenant."""

    def __init__(
        self,
        code: str,
        customer_name: str,
        message: str,
        *,
        requested_name: str | None = None,
        resolved_name: str | None = None,
        match_type: str = "none",
        candidates: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.customer_name = customer_name
        self.message = message
        self._init_tenant_context(
            requested_name=(
                requested_name if requested_name is not None else customer_name
            ),
            resolved_name=resolved_name,
            match_type=match_type,
        )
        self.candidates = candidates or []
        self.details = details or {}

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.code,
            "customer_name": self.customer_name,
            "message": self.message,
        }
        result.update(self.tenant_context_result())
        if self.candidates:
            result["candidates"] = self.candidates
        if self.details:
            result["details"] = self.details
        return result


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
    根據 registry 中的正式名稱或 exact managed alias 取得 GA4 BigQuery 的位置。
    """

    row, requested_name, resolved_name, match_type = _resolve_tenant_record(
        client,
        customer_name,
    )
    tenant_status = (row.status or "").strip().lower()

    if tenant_status != "active":
        raise TenantResolutionError(
            "tenant_inactive",
            requested_name,
            f"客戶「{row.tenant_name}」存在，但目前狀態為 {tenant_status or '未設定'}，尚未開放查詢。",
            requested_name=requested_name,
            resolved_name=row.tenant_name,
            match_type=match_type,
        )

    if not row.project_id:
        raise TenantResolutionError(
            "data_unavailable",
            requested_name,
            f"客戶「{row.tenant_name}」存在，但尚未設定 GA4 BigQuery 專案。",
            requested_name=requested_name,
            resolved_name=row.tenant_name,
            match_type=match_type,
        )

    # table identifier 無法使用 BigQuery query parameter，
    # 所以在放進 SQL 前先限制格式。
    identifier_pattern = r"[A-Za-z0-9_\-]+"

    if not re.fullmatch(identifier_pattern, row.project_id):
        raise TenantResolutionError(
            "data_unavailable",
            requested_name,
            f"客戶「{row.tenant_name}」的 GA4 BigQuery 專案設定無效。",
            requested_name=requested_name,
            resolved_name=resolved_name,
            match_type=match_type,
        )

    return {
        "tenant_id": row.tenant_id,
        "tenant_name": row.tenant_name,
        "requested_name": requested_name,
        "resolved_name": resolved_name,
        "match_type": match_type,
        "project_id": row.project_id,
        "dataset_id": "ga4_mar",
        # Registry policy: only literal TRUE means ecommerce. FALSE and blank
        # retain the non-ecommerce behavior used before the column existed.
        "semantic_profile": "ecommerce" if row.ec is True else "non_ecommerce",
    }


def _registry_row_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _registry_match_type(row: Any, requested_normalized: str) -> str:
    explicit_match_type = _registry_row_value(row, "match_type")
    if explicit_match_type:
        return str(explicit_match_type)
    formal_name = _registry_row_value(row, "tenant_name", "")
    if isinstance(formal_name, str):
        normalized_formal_name = normalize_customer_name(formal_name)
        if normalized_formal_name == requested_normalized:
            return "exact"
        aliases = _registry_row_value(row, "aliases")
        if isinstance(aliases, str):
            if requested_normalized in {
                normalize_customer_name(alias)
                for alias in aliases.split(ALIAS_SEPARATOR)
                if alias.strip()
            }:
                return "alias"
        if requested_normalized in normalized_formal_name:
            return "partial"
    return "partial"


def _candidate_summary(row: Any) -> dict[str, Any]:
    tenant_name = _registry_row_value(row, "tenant_name")
    status = (_registry_row_value(row, "status", "") or "").strip().lower()
    project_id = _registry_row_value(row, "project_id")
    return {
        "tenant_name": tenant_name,
        "resolved_name": tenant_name,
        "tenant_status": status or "unset",
        "analytics_available": status == "active" and bool(project_id),
    }


def _raise_candidate_error(
    *,
    requested_name: str,
    candidates: list[Any],
) -> None:
    candidate_results = [_candidate_summary(row) for row in candidates]
    if is_broad_customer_search(requested_name):
        raise TenantResolutionError(
            "customer_name_too_broad",
            requested_name,
            f"客戶名稱「{requested_name}」過短或過於通用，請提供更完整的正式名稱。",
            requested_name=requested_name,
            match_type="partial",
            candidates=candidate_results,
        )
    if len(candidates) == 1:
        resolved_name = _registry_row_value(candidates[0], "tenant_name")
        raise TenantResolutionError(
            "tenant_confirmation_required",
            requested_name,
            f"「{requested_name}」可能是客戶「{resolved_name}」，請確認正式名稱後再查詢。",
            requested_name=requested_name,
            resolved_name=resolved_name,
            match_type="partial",
            candidates=candidate_results,
        )
    raise TenantResolutionError(
        "ambiguous_tenant",
        requested_name,
        f"客戶名稱「{requested_name}」對應多個候選，請確認正式名稱後再查詢。",
        requested_name=requested_name,
        match_type="partial",
        candidates=candidate_results,
    )


def _resolve_tenant_record(
    client: bigquery.Client,
    customer_name: str,
):
    """Resolve formal name, exact alias, or return safe partial candidates."""

    if not isinstance(customer_name, str):
        raise TenantResolutionError(
            "invalid_customer_name",
            str(customer_name),
            "請提供客戶名稱。",
        )
    requested_name = customer_name.strip()
    requested_normalized = normalize_customer_name(requested_name)
    if not requested_normalized:
        raise TenantResolutionError(
            "invalid_customer_name",
            requested_name,
            "請提供客戶名稱。",
            requested_name=requested_name,
        )

    sql = f"""
    WITH named_tenants AS (
      SELECT
        tenant_id,
        tenant_name,
        project_id,
        status,
        ec,
        aliases,
        NORMALIZE_AND_CASEFOLD(TRIM(tenant_name), NFKC) AS normalized_name,
        NORMALIZE_AND_CASEFOLD(@customer_name, NFKC) AS normalized_request
      FROM `{REGISTRY_TABLE}`
      WHERE NULLIF(TRIM(tenant_name), '') IS NOT NULL
    ),
    alias_matches AS (
      SELECT
        tenant_id,
        tenant_name,
        project_id,
        status,
        ec,
        aliases,
        normalized_name,
        'alias' AS match_type
      FROM named_tenants
      CROSS JOIN UNNEST(SPLIT(COALESCE(aliases, ''), '{ALIAS_SEPARATOR}')) AS alias
      WHERE NULLIF(TRIM(alias), '') IS NOT NULL
        AND NORMALIZE_AND_CASEFOLD(TRIM(alias), NFKC) = normalized_request
    ),
    matches AS (
      SELECT
        tenant_id,
        tenant_name,
        project_id,
        status,
        ec,
        aliases,
        normalized_name,
        'exact' AS match_type,
        0 AS match_order
      FROM named_tenants
      WHERE normalized_name = normalized_request
      UNION ALL
      SELECT
        tenant_id,
        tenant_name,
        project_id,
        status,
        ec,
        aliases,
        normalized_name,
        match_type,
        1 AS match_order
      FROM alias_matches
      UNION ALL
      SELECT
        tenant_id,
        tenant_name,
        project_id,
        status,
        ec,
        aliases,
        normalized_name,
        'partial' AS match_type,
        2 AS match_order
      FROM named_tenants
      WHERE STRPOS(normalized_name, normalized_request) > 0
    )
    SELECT
      tenant_id,
      tenant_name,
      project_id,
      status,
      ec,
      aliases,
      match_type
    FROM matches
    ORDER BY match_order, normalized_name, tenant_id
    LIMIT 22
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "customer_name",
                "STRING",
                requested_name,
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
            mapped_error.attach_request_context(
                TenantRequestContext.from_customer_name(requested_name)
            )
            raise mapped_error from error
        raise

    typed_rows = [
        (row, _registry_match_type(row, requested_normalized))
        for row in rows
    ]
    exact_rows = [row for row, match_type in typed_rows if match_type == "exact"]
    if exact_rows:
        if len(exact_rows) > 1:
            raise TenantResolutionError(
                "ambiguous_tenant",
                requested_name,
                f"客戶名稱「{requested_name}」對應到多筆 tenant，請聯絡管理者修正 registry。",
                requested_name=requested_name,
                match_type="exact",
                candidates=[_candidate_summary(row) for row in exact_rows],
            )
        row = exact_rows[0]
        return row, requested_name, _registry_row_value(row, "tenant_name"), "exact"

    alias_rows = [row for row, match_type in typed_rows if match_type == "alias"]
    if alias_rows:
        if len(alias_rows) > 1:
            raise TenantResolutionError(
                "ambiguous_tenant",
                requested_name,
                f"alias「{requested_name}」對應到多個 tenant，請聯絡管理者修正 registry。",
                requested_name=requested_name,
                match_type="alias",
                candidates=[_candidate_summary(row) for row in alias_rows],
            )
        row = alias_rows[0]
        return row, requested_name, _registry_row_value(row, "tenant_name"), "alias"

    partial_rows = [row for row, match_type in typed_rows if match_type == "partial"]
    if partial_rows:
        _raise_candidate_error(
            requested_name=requested_name,
            candidates=partial_rows,
        )

    if not rows:
        raise TenantResolutionError(
            "tenant_not_found",
            requested_name,
            f"tenant registry 中不存在客戶「{requested_name}」。",
            requested_name=requested_name,
        )

    raise TenantResolutionError(
        "tenant_not_found",
        requested_name,
        f"tenant registry 中不存在客戶「{requested_name}」。",
        requested_name=requested_name,
    )


def get_tenant_record(client: bigquery.Client, customer_name: str):
    """Compatibility wrapper returning a resolved row and requested name."""

    row, requested_name, _, _ = _resolve_tenant_record(client, customer_name)
    return row, requested_name


def get_customer_status(customer_name: str) -> dict:
    """Report registry existence independently from GA4 dataset availability."""

    client = get_bigquery_client()
    row, requested_name, resolved_name, match_type = _resolve_tenant_record(
        client,
        customer_name,
    )
    tenant_status = (row.status or "").strip().lower()
    analytics_available = tenant_status == "active" and bool(row.project_id)
    return {
        "status": "customer_found",
        "customer_name": row.tenant_name,
        "requested_name": requested_name,
        "resolved_name": resolved_name,
        "match_type": match_type,
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


def get_ga4_capability_resolution(request: str | None = None) -> dict[str, Any]:
    """Resolve connector capabilities from local versioned metadata only."""

    return capability_registry.resolve(request)


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


def _query_provenance_result(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": QUERY_PROVENANCE_SCHEMA_VERSION,
        "queries": records,
    }


def _attach_provenance_if_requested(
    error: Exception,
    records: list[dict[str, Any]],
    *,
    include_query: bool,
) -> None:
    if include_query:
        attach_query_provenance(error, records)


def _query_job_for_error(error: Exception, fallback: Any = None) -> Any:
    """Keep the accepted BigQuery job when a later iterator operation fails."""

    return getattr(error, "_query_job", None) or fallback


def query_ga4_semantic_metrics(
    customer_name: str,
    metric_ids: list[str],
    start_date: str,
    end_date: str,
    limit: int = 50,
    include_query: bool = False,
) -> dict[str, Any]:
    """Execute catalog-approved metric SQL for one resolved tenant."""

    request_context = TenantRequestContext.from_customer_name(customer_name)
    try:
        return _query_ga4_semantic_metrics(
            customer_name=customer_name,
            metric_ids=metric_ids,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            include_query=include_query,
            request_context=request_context,
        )
    except (
        TenantResolutionError,
        QueryPolicyError,
        SemanticCatalogError,
    ) as error:
        error.attach_request_context(request_context)
        raise


def _query_ga4_semantic_metrics(
    customer_name: str,
    metric_ids: list[str],
    start_date: str,
    end_date: str,
    limit: int,
    include_query: bool,
    request_context: TenantRequestContext,
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
    semantic_catalog.find_publishable_profiles(normalized_metric_ids)
    result_limit = max(1, min(int(limit), 200))
    client = get_bigquery_client()
    tenant = get_tenant_config(client, customer_name)
    request_context.resolve_from_tenant(tenant)
    resolved_profile, _ = semantic_catalog.resolve_profile(
        normalized_metric_ids,
        tenant["semantic_profile"],
    )
    profile_resolution = "tenant_registry.ec"
    prepared_metrics: list[dict[str, Any]] = []
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
        query_parameters: list[Any] = []
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
                    catalog_version=semantic_catalog.version,
                ),
            }
        )

    queries: list[PreparedQuery] = [item["query"] for item in prepared_metrics]
    try:
        estimates = query_policy.preflight_request(client, queries)
    except QueryPolicyError as error:
        records = [
            build_query_provenance(
                item["query"],
                status="not_executed",
                estimated_bytes_processed=(
                    error.details.get("estimated_bytes")
                    if error.details.get("query") == item["metric_id"]
                    else None
                ),
            )
            for item in prepared_metrics
        ]
        _attach_provenance_if_requested(
            error,
            records,
            include_query=include_query,
        )
        raise

    metric_results: list[dict[str, Any]] = []
    executed_records: list[dict[str, Any]] = []
    for index, item in enumerate(prepared_metrics):
        metric_id = item["metric_id"]
        metric = item["metric"]
        query_job = None
        try:
            query_job, rows = query_policy.execute(client, item["query"])
            rows = list(rows)
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
        except QueryPolicyError as error:
            records = [
                *executed_records,
                build_query_provenance(
                    item["query"],
                    job=_query_job_for_error(error, query_job),
                    status="failed",
                    estimated_bytes_processed=estimates.get(metric_id),
                ),
                *(
                    build_query_provenance(
                        future_item["query"],
                        status="not_executed",
                        estimated_bytes_processed=estimates.get(
                            future_item["metric_id"]
                        ),
                    )
                    for future_item in prepared_metrics[index + 1 :]
                ),
            ]
            _attach_provenance_if_requested(
                error,
                records,
                include_query=include_query,
            )
            raise
        except Exception as error:
            semantic_error = SemanticCatalogError(
                "data_unavailable",
                f"客戶「{tenant['tenant_name']}」的指標「{metric_id}」目前無法查詢。",
                details={"metric_id": metric_id},
            )
            records = [
                *executed_records,
                build_query_provenance(
                    item["query"],
                    job=_query_job_for_error(error, query_job),
                    status="failed",
                    estimated_bytes_processed=estimates.get(metric_id),
                ),
                *(
                    build_query_provenance(
                        future_item["query"],
                        status="not_executed",
                        estimated_bytes_processed=estimates.get(
                            future_item["metric_id"]
                        ),
                    )
                    for future_item in prepared_metrics[index + 1 :]
                ),
            ]
            _attach_provenance_if_requested(
                semantic_error,
                records,
                include_query=include_query,
            )
            raise semantic_error from error

        executed_records.append(
            build_query_provenance(
                item["query"],
                job=query_job,
                estimated_bytes_processed=estimates.get(metric_id),
            )
        )

    result = {
        "status": "ok",
        "tenant": {
            "tenant_id": tenant["tenant_id"],
            "tenant_name": tenant["tenant_name"],
            "requested_name": tenant["requested_name"],
            "resolved_name": tenant["resolved_name"],
            "match_type": tenant["match_type"],
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
    if include_query:
        result["query_provenance"] = _query_provenance_result(executed_records)
    return result


def get_traffic_summary(
    customer_name: str,
    start_date: str,
    end_date: str,
    include_query: bool = False,
):
    """Build one traffic summary with consistent tenant error context."""

    request_context = TenantRequestContext.from_customer_name(customer_name)
    try:
        return _get_traffic_summary(
            customer_name=customer_name,
            start_date=start_date,
            end_date=end_date,
            include_query=include_query,
            request_context=request_context,
        )
    except (
        TenantResolutionError,
        QueryPolicyError,
        TrafficSummaryReportError,
    ) as error:
        error.attach_request_context(request_context)
        raise


def _get_traffic_summary(
    customer_name: str,
    start_date: str,
    end_date: str,
    include_query: bool,
    request_context: TenantRequestContext,
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
    request_context.resolve_from_tenant(tenant)

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
        catalog_version=semantic_catalog.version,
    )

    try:
        estimates = query_policy.preflight_request(client, [prepared_query])
    except QueryPolicyError as error:
        _attach_provenance_if_requested(
            error,
            [
                build_query_provenance(
                    prepared_query,
                    status="not_executed",
                    estimated_bytes_processed=error.details.get(
                        "estimated_bytes"
                    ),
                )
            ],
            include_query=include_query,
        )
        raise

    query_job = None
    try:
        query_job, rows = query_policy.execute(client, prepared_query)
    except QueryPolicyError as error:
        _attach_provenance_if_requested(
            error,
            [
                build_query_provenance(
                    prepared_query,
                    job=_query_job_for_error(error, query_job),
                    status="failed",
                    estimated_bytes_processed=estimates.get("traffic_summary"),
                )
            ],
            include_query=include_query,
        )
        raise
    except Exception as error:
        mapped_error = TenantResolutionError(
            "data_unavailable",
            customer_name,
            f"客戶「{tenant['tenant_name']}」存在於 tenant registry，但目前無法取得 GA4 流量資料。",
            requested_name=tenant["requested_name"],
            resolved_name=tenant["resolved_name"],
            match_type=tenant["match_type"],
        )
        _attach_provenance_if_requested(
            mapped_error,
            [
                build_query_provenance(
                    prepared_query,
                    job=_query_job_for_error(error, query_job),
                    status="failed",
                    estimated_bytes_processed=estimates.get("traffic_summary"),
                )
            ],
            include_query=include_query,
        )
        raise mapped_error from error

    successful_record = build_query_provenance(
        prepared_query,
        job=query_job,
        estimated_bytes_processed=estimates.get("traffic_summary"),
    )

    try:
        row_iterator = iter(rows)
        row = next(row_iterator)
        has_extra_row = next(row_iterator, None) is not None
    except StopIteration as error:
        report_error = TrafficSummaryReportError()
        _attach_provenance_if_requested(
            report_error,
            [successful_record],
            include_query=include_query,
        )
        raise report_error from error
    except Exception as error:
        report_error = TrafficSummaryReportError()
        _attach_provenance_if_requested(
            report_error,
            [
                build_query_provenance(
                    prepared_query,
                    job=query_job,
                    status="failed",
                    estimated_bytes_processed=estimates.get("traffic_summary"),
                )
            ],
            include_query=include_query,
        )
        raise report_error from error
    if has_extra_row:
        report_error = TrafficSummaryReportError()
        _attach_provenance_if_requested(
            report_error,
            [successful_record],
            include_query=include_query,
        )
        raise report_error

    try:
        result = build_traffic_summary_report(
            row=row,
            tenant=tenant,
        )
    except TrafficSummaryReportError as error:
        _attach_provenance_if_requested(
            error,
            [successful_record],
            include_query=include_query,
        )
        raise
    if include_query:
        result["query_provenance"] = _query_provenance_result([successful_record])
    return result


@app.get(
    "/traffic-summary",
    dependencies=[Depends(require_rest_oauth)],
)
def traffic_summary(
    customer_name: str,
    start_date: str,
    end_date: str,
    include_query: bool = False,
):
    try:
        return get_traffic_summary(
            customer_name=customer_name,
            start_date=start_date,
            end_date=end_date,
            include_query=include_query,
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
    except TrafficSummaryReportError as error:
        raise HTTPException(
            status_code=502,
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
