from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any

from semantic_catalog import SemanticCatalog, semantic_catalog


CAPABILITY_REGISTRY_VERSION = "1.0.0"


@dataclass(frozen=True)
class UnsupportedIntent:
    intent_id: str
    label: str
    patterns: tuple[re.Pattern[str], ...]
    message: str

    def matches(self, request: str) -> bool:
        return any(pattern.search(request) for pattern in self.patterns)


PUBLIC_TOOL_DESCRIPTIONS = {
    "customer_lookup": """
Check whether an exact customer name exists in the tenant registry.

Use this tool whenever the user asks whether a customer exists. This lookup
does not require access to the customer's GA4 dataset. A customer can exist
even when analytics_available is false. If the result is tenant_not_found,
do not claim that a similar customer exists and do not guess another name.
When configured, data_source contains the project_id and dataset_id for
internal routing. Never ask the user to provide either identifier.
""".strip(),
    "list_available_customers": """
List customer names currently available for GA4 traffic summary queries.

Use this tool when the user asks which customers or accounts can be queried.
It returns only registry entries that are active, have a configured project,
have a non-empty customer name, and can be uniquely resolved by that name.
Present the customer names directly. Do not expose tenant IDs or project IDs,
and do not ask the user to inspect the registry spreadsheet instead.
""".strip(),
    "get_ga4_capabilities": """
Resolve whether a requested analysis is supported, unsupported, or needs clarification.

Call this before accessing customer data when the requested data source or
analysis type is uncertain. It uses only local, versioned capability metadata
and the semantic catalog; it never queries the tenant registry or BigQuery.
Google Ads spend, SEO keyword ranking, CRM data, arbitrary SQL, and write
operations are unsupported. GA4 traffic summary and published catalog metrics
are supported. Follow the returned resolution and next_action; do not replace
an unsupported result with general knowledge or inferred customer data.
""".strip(),
    "search_ga4_metrics": """
Search the versioned GA4 semantic catalog for supported metric IDs.

Use this before query_ga4 whenever the user asks for an analysis beyond the
fixed traffic summary or uses a natural-language metric name. The search
covers metric labels, report context, and dimensions such as source, medium,
campaign, date, device, geography, page, and item. Pass profile as ecommerce
or non_ecommerce only when the website type is already known. This tool reads
definitions only; it does not query the tenant registry or customer data.
""".strip(),
    "query_ga4": """
Query one to five published GA4 semantic metrics for a customer and period.

metric_ids must come from search_ga4_metrics; never invent IDs. Even if the
model skips search, the server validates that all IDs have a publishable local
catalog profile before it creates a BigQuery client or queries the tenant
registry. It then resolves project_id, dataset_id, and ecommerce profile from
the registry and compiles only catalog-approved SQL. It never accepts raw
table, column, filter, group by, profile, or SQL input. Present only rows
returned with status ok and retain routing metadata as internal context.
Respect each metric's date_scope: do not describe an all_available_data result
as limited to start_date and end_date. The server enforces its configured
date, per-job, request-total, timeout, and daily BigQuery cost limits; relay a
structured limit error instead of retrying around it.
""".strip(),
    "traffic_summary": """
Get GA4 traffic summary by the customer's registered name and date range.

Returns current period, previous period, and percentage change for total
sessions, total users, new users, and returning users. Always use the customer
name stated by the user. If the result status is tenant_not_found, tell the
user that the customer does not exist in the tenant registry. If it is
tenant_inactive, explain that the customer exists but is not currently
available. Never guess a different customer. The result includes data_source
routing metadata; retain it as context and never ask the user for project_id
or dataset_id. For supported follow-up analyses such as source, medium, or
campaign, use search_ga4_metrics and query_ga4 rather than claiming arbitrary
BigQuery access. The same shared date and BigQuery cost policy applies to this
tool and the REST endpoint.
""".strip(),
}


SERVER_INSTRUCTIONS = """
This server resolves every customer name through the tenant registry. Users
never need to know or provide tenant_id, project_id, or dataset_id. Use the
customer name from the conversation in each data tool call; tool results
include data_source routing metadata when it is configured. Treat tenant_id,
project_id, and dataset_id as internal metadata and do not show them in the
answer unless the user explicitly asks for technical routing details.

When the requested data source or analysis type is uncertain, first call
get_ga4_capabilities. It performs local metadata lookup only. Follow its
resolution exactly: unsupported means explain the boundary without querying
customer data; needs_clarification means ask a focused question; supported
means follow next_action. For example, Google Ads spend, SEO keyword ranking,
CRM records, arbitrary SQL, and data modification are unsupported. GA4 traffic
summary and published semantic catalog metrics are supported. A request such
as "analyze performance" needs clarification about the GA4 metric and period.

When the user asks which customers are available, call
list_available_customers and present its customer names. Do not replace the
customer list with a registry spreadsheet link.

For analytics beyond traffic_summary, first use search_ga4_metrics to find the
published metric IDs in the versioned semantic catalog, then call query_ga4
with only those IDs. Catalog metrics may include source, medium, campaign,
content, conversion, and ecommerce analyses. Never invent a metric ID or SQL.
query_ga4 resolves ecommerce versus non-ecommerce from the tenant registry ec
field; never ask the user to identify the site type. Never ask the user for a
project ID or dataset ID to work around a missing capability.

Each semantic metric result includes date_scope. If it is all_available_data,
state that the metric definition is an all-data snapshot and do not describe it
as limited to the requested period.

Never present general knowledge or an inference as actual customer data, and
never claim a catalog metric was queried unless a tool returned status ok.
""".strip()


class CapabilityRegistry:
    """Versioned, local source of truth for connector capability boundaries."""

    def __init__(self, catalog: SemanticCatalog):
        self.catalog = catalog
        self.version = CAPABILITY_REGISTRY_VERSION
        self.unsupported_intents = (
            UnsupportedIntent(
                "google_ads",
                "Google Ads 媒體資料",
                (
                    re.compile(r"\bgoogle\s*ads?\b"),
                    re.compile(
                        r"(?:廣告|ads?).{0,12}(?:花費|費用|成本|spend|cost|cpc|cpm|roas|曝光|點擊)"
                    ),
                ),
                "目前只提供 GA4 資料，不提供 Google Ads 花費、曝光或廣告成效資料。",
            ),
            UnsupportedIntent(
                "seo_keyword_ranking",
                "SEO keyword ranking",
                (
                    re.compile(r"(?:seo|搜尋).{0,12}(?:關鍵字|keyword|排名|ranking)"),
                    re.compile(r"(?:關鍵字|keyword).{0,12}(?:排名|ranking)"),
                    re.compile(r"search\s*console"),
                ),
                "目前不提供 Search Console 或 SEO 關鍵字排名資料。",
            ),
            UnsupportedIntent(
                "crm",
                "CRM 資料",
                (re.compile(r"\bcrm\b|客戶關係管理"),),
                "目前不提供 CRM、名單或銷售管線資料。",
            ),
            UnsupportedIntent(
                "arbitrary_bigquery",
                "任意 BigQuery / SQL",
                (
                    re.compile(r"(?:任意|自訂|直接).{0,8}(?:bigquery|sql)"),
                    re.compile(r"(?:執行|run).{0,8}(?:sql|query)"),
                ),
                "目前只執行已發布的 GA4 catalog query，不接受任意 SQL。",
            ),
            UnsupportedIntent(
                "data_modification",
                "資料修改",
                (
                    re.compile(
                        r"(?:修改|寫入|新增|刪除|更新).{0,12}(?:資料|紀錄|bigquery|table|表格)"
                    ),
                    re.compile(r"\b(?:insert|update|delete|merge|drop)\b"),
                ),
                "此 connector 為唯讀服務，不提供資料寫入、修改或刪除能力。",
            ),
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return unicodedata.normalize("NFKC", value).casefold().strip()

    def public_tool_names(self) -> tuple[str, ...]:
        return tuple(PUBLIC_TOOL_DESCRIPTIONS)

    def tool_description(self, tool_name: str) -> str:
        return PUBLIC_TOOL_DESCRIPTIONS[tool_name]

    def inventory(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "registry_version": self.version,
            "catalog_version": self.catalog.version,
            "data_access": "local_metadata_only",
            "selection_token_required": False,
            "supported": [
                {
                    "capability_id": "customer_discovery",
                    "data_source": "tenant_registry",
                    "tools": ["customer_lookup", "list_available_customers"],
                },
                {
                    "capability_id": "ga4_traffic_summary",
                    "data_source": "ga4",
                    "tools": ["traffic_summary"],
                },
                {
                    "capability_id": "ga4_semantic_metrics",
                    "data_source": "ga4",
                    "tools": ["search_ga4_metrics", "query_ga4"],
                },
            ],
            "unsupported": [
                {"intent_id": intent.intent_id, "label": intent.label}
                for intent in self.unsupported_intents
            ],
            "public_tools": list(self.public_tool_names()),
            "limitations": [
                "GA4 semantic catalog 中已發布的唯讀 metrics",
                "客戶、profile、project 與 dataset 只能由 tenant registry 解析",
                "日期、單一 job、request 合計、timeout 與每日 BigQuery quota 限制",
                "不接受任意 SQL 或資料修改",
            ],
        }

    def resolve(self, request: str | None = None) -> dict[str, Any]:
        if request is None:
            return self.inventory()
        if not isinstance(request, str) or not request.strip():
            return self._resolution(
                request=request if isinstance(request, str) else "",
                resolution="needs_clarification",
                reason_code="missing_analysis_request",
                message="請說明要查詢的 GA4 指標或分析目的。",
                next_action={
                    "type": "ask_user",
                    "question": "你想查看哪個 GA4 指標、客戶與日期範圍？",
                },
            )

        normalized_request = self._normalize(request)
        for intent in self.unsupported_intents:
            if intent.matches(normalized_request):
                return self._resolution(
                    request=request,
                    resolution="unsupported",
                    reason_code=intent.intent_id,
                    message=intent.message,
                    next_action={"type": "explain_boundary"},
                )

        if re.search(r"流量摘要|traffic\s+summary", normalized_request):
            return self._resolution(
                request=request,
                resolution="supported",
                reason_code="ga4_traffic_summary",
                message="可使用固定的 GA4 traffic summary 查詢。",
                next_action={"type": "call_tool", "tool": "traffic_summary"},
            )

        search_result = self.catalog.search(request, limit=10)
        if search_result["metrics"]:
            return self._resolution(
                request=request,
                resolution="supported",
                reason_code="ga4_semantic_metric",
                message="本機 semantic catalog 有可用的 GA4 metric 候選。",
                next_action={"type": "call_tool", "tool": "search_ga4_metrics"},
                metric_candidates=search_result["metrics"],
            )

        return self._resolution(
            request=request,
            resolution="needs_clarification",
            reason_code="no_local_capability_match",
            message="目前無法從本機 capability metadata 判斷要使用的 GA4 metric。",
            next_action={
                "type": "ask_user",
                "question": "請指定想查看的 GA4 指標或分析維度。",
            },
        )

    def _resolution(
        self,
        *,
        request: str,
        resolution: str,
        reason_code: str,
        message: str,
        next_action: dict[str, Any],
        metric_candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "ok",
            "registry_version": self.version,
            "catalog_version": self.catalog.version,
            "data_access": "local_metadata_only",
            "request": request,
            "resolution": resolution,
            "reason_code": reason_code,
            "message": message,
            "next_action": next_action,
        }
        if metric_candidates is not None:
            result["metric_candidates"] = metric_candidates
        return result


capability_registry = CapabilityRegistry(semantic_catalog)
