from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any

from semantic_catalog import SemanticCatalog, semantic_catalog


CAPABILITY_REGISTRY_VERSION = "1.1.0"


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
Google Ads or Meta/Facebook Ads performance, SEO keyword ranking, external CRM
or lead-list data, arbitrary SQL, weather, and write operations are unsupported.
GA4 generate_lead and lead-conversion events remain valid catalog metrics. A
fuzzy catalog hit alone never proves support: GA4 traffic summary or an explicit
GA4 metric request is required. Follow the returned resolution and next_action;
do not replace an unsupported result with general knowledge or inferred customer
data.
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
sessions, total users, new users, and returning users. When status is ok,
render the result strictly as the versioned presentation contract declares:
a line_chart with small_multiples layout, one chart per metric, and exactly
the current and previous series declared by each chart. Use daily_series directly;
preserve its source-date x-axis and day_index comparison alignment,
and do not replace the report with a table, infer values, or add another
series. Always use the customer name stated by the user. If the result status
is tenant_not_found, tell the user that the customer does not exist in the
tenant registry. If status is tenant_inactive, explain that the customer exists
but is not currently available. Never guess a different customer. The result
includes data_source routing metadata; retain it as context and never ask the user for project_id
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
means follow next_action. For example, Google Ads or Meta/Facebook Ads
performance, SEO keyword ranking, external CRM or lead-list records, arbitrary
SQL, weather, and data modification are unsupported. Published GA4
generate_lead and lead-conversion event metrics are supported. GA4 traffic
summary and explicit GA4 metric requests with published semantic catalog
candidates are supported. A request such as "traffic" or "analyze SEO
performance" needs clarification; a fuzzy catalog hit by itself must never be
treated as proof that a request is supported. Resolve every affirmative part of
a mixed request before accepting a catalog hit. Honor explicit exclusions such
as "do not query Ads, query GA4 sessions" by resolving the remaining affirmative
GA4 request.

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

For a successful traffic_summary result, follow its presentation contract
exactly: render a line chart with small_multiples layout, one chart per metric,
and only the current and previous series declared there. Use daily_series as
the chart data, preserve its source-date x-axis and day_index comparison
alignment, and do not replace the report with a table or invent missing data.

Never present general knowledge or an inference as actual customer data, and
never claim a catalog metric was queried unless a tool returned status ok.
""".strip()


class CapabilityRegistry:
    """Versioned, local source of truth for connector capability boundaries."""

    def __init__(self, catalog: SemanticCatalog):
        self.catalog = catalog
        self.version = CAPABILITY_REGISTRY_VERSION
        ads_platform = r"(?:google\s*ads?|meta\s*ads?|facebook\s*ads?|fb\s*廣告)"
        ads_native_metric = (
            r"(?:廣告|成效|花費|費用|成本|spend|cost|performance|cpc|cpm|roas|"
            r"ctr|clicks?|impressions?|conversions?|campaign\s+reports?|data|reports?|"
            r"曝光|點擊|轉換|資料|報表|報告)"
        )
        self._ads_exclusive_metric_pattern = re.compile(
            r"(?:成效|花費|費用|成本|spend|cost|performance|cpc|cpm|roas|ctr|"
            r"clicks?|impressions?|campaign\s+reports?|data|reports?|"
            r"曝光|點擊|資料|報表|報告)"
        )
        ga4_source = r"(?:ga\s*4|google\s*analytics)"
        ga4_attribution_metric = (
            r"(?:sessions?|users?|conversions?|revenue|工作階段|使用者|轉換|收益)"
        )
        attribution_connector = r"(?:from|by|attributed\s+to|來自|來源|歸因(?:於|到)?)"
        self._explicit_ga4_ads_attribution_pattern = re.compile(
            rf"{ga4_source}(?:"
            rf".{{0,32}}{ga4_attribution_metric}.{{0,12}}"
            rf"{attribution_connector}.{{0,12}}{ads_platform}|"
            rf".{{0,12}}{attribution_connector}.{{0,12}}{ads_platform}"
            rf".{{0,12}}{ga4_attribution_metric}"
            rf")"
        )
        crm_platform = r"(?:crm|salesforce|hubspot)"
        self._crm_exclusive_metric_pattern = re.compile(
            r"(?:records?|lists?|leads?|opportunit(?:y|ies)|pipeline|"
            r"customer\s+lifetime\s+value|資料|紀錄|記錄|名單|清單|銷售管線|客戶終身價值)"
        )
        self._explicit_ga4_crm_attribution_pattern = re.compile(
            rf"{ga4_source}(?:"
            rf".{{0,32}}{ga4_attribution_metric}.{{0,12}}"
            rf"{attribution_connector}.{{0,12}}{crm_platform}|"
            rf".{{0,12}}{attribution_connector}.{{0,12}}{crm_platform}"
            rf".{{0,20}}{ga4_attribution_metric}"
            rf")"
        )
        self._explicit_ga4_site_search_pattern = re.compile(
            rf"{ga4_source}.{{0,20}}(?:站內搜尋(?:關鍵字|字詞)|site\s+search\s+terms?|"
            r"search\s+terms?)"
        )
        self.unsupported_intents = (
            UnsupportedIntent(
                "advertising_data",
                "Google Ads／Meta／Facebook 廣告資料",
                (
                    re.compile(
                        rf"(?<![a-z0-9]){ads_platform}(?![a-z0-9]).{{0,20}}"
                        rf"{ads_native_metric}"
                    ),
                    re.compile(
                        rf"{ads_native_metric}.{{0,20}}"
                        rf"(?<![a-z0-9]){ads_platform}(?![a-z0-9])"
                    ),
                    re.compile(
                        r"(?:不要排除|不是不要|沒有說不要|没有说不要).{0,12}"
                        rf"{ads_platform}"
                    ),
                    re.compile(
                        r"(?:廣告|(?<![a-z])ads?(?![a-z])).{0,12}"
                        rf"{ads_native_metric}"
                    ),
                ),
                "目前只提供 GA4 資料，不提供 Google Ads、Meta 或 Facebook 的媒體成效資料。",
            ),
            UnsupportedIntent(
                "seo_keyword_ranking",
                "SEO keyword ranking",
                (
                    re.compile(
                        r"(?:seo|搜尋引擎).{0,12}(?:關鍵字|keyword|排名|ranking)"
                    ),
                    re.compile(r"(?:關鍵字|keyword).{0,12}(?:排名|ranking)"),
                    re.compile(r"search\s*console"),
                ),
                "目前不提供 Search Console 或 SEO 關鍵字排名資料。",
            ),
            UnsupportedIntent(
                "crm",
                "CRM／名單／銷售管線資料",
                (
                    re.compile(
                        r"(?<![a-z0-9])crm(?![a-z0-9])|客戶關係管理|銷售管線|"
                        r"sales\s*pipeline|salesforce|hubspot|"
                        r"(?<![a-z])opportunit(?:y|ies)(?![a-z])"
                    ),
                    re.compile(
                        r"(?:不要排除|不是不要|沒有說不要|没有说不要).{0,12}"
                        r"(?:crm|salesforce|hubspot|客戶關係管理|銷售管線|名單|leads?)"
                    ),
                    re.compile(
                        r"(?:查|看|取得|匯出|我要).{0,8}"
                        r"(?:名單|(?<![a-z])leads?(?![a-z]))"
                    ),
                    re.compile(
                        r"(?<![a-z])(?:fetch|show(?:\s+me)?|download|get|export)"
                        r".{0,12}(?:leads?|lead\s+list)(?![a-z])"
                    ),
                    re.compile(
                        r"(?:名單|(?<![a-z])leads?(?![a-z])).{0,8}"
                        r"(?:資料|清單|列表|records?|list)"
                    ),
                    re.compile(r"客戶名單"),
                ),
                "目前不提供 CRM、名單或銷售管線資料。",
            ),
            UnsupportedIntent(
                "arbitrary_bigquery",
                "任意 BigQuery / SQL",
                (
                    re.compile(r"(?:任意|自訂|直接|幫我|替我).{0,8}(?:bigquery|sql)"),
                    re.compile(
                        r"(?<![a-z])custom\s+(?:bigquery|sql)(?:\s+query)?(?![a-z])"
                    ),
                    re.compile(r"(?<![a-z])raw\s+sql(?![a-z])"),
                    re.compile(r"(?:寫|產生|生成).{0,8}sql"),
                    re.compile(
                        r"(?:執行|run).{0,12}(?:raw|custom|任意|自訂).{0,8}(?:sql|query)"
                    ),
                    re.compile(
                        r"(?<![a-z])(?:run|execute).{0,12}(?:sql|bigquery)(?![a-z])"
                    ),
                    re.compile(r"(?:執行|run)\s+(?:select|with)\b"),
                    re.compile(r"\bselect\s+\*"),
                    re.compile(r"\bselect\b.{0,120}\bfrom\b"),
                    re.compile(r"\bwith\s+[a-z_][a-z0-9_]*\s+as\s*\("),
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
                    re.compile(
                        r"(?:清空|抹除|清除|銷毀).{0,12}(?:資料|紀錄|記錄|table|表格)"
                    ),
                    re.compile(r"\binsert\s+into\b|\bdelete\s+from\b|\bmerge\s+into\b"),
                    re.compile(r"\bupdate\s+(?:table\s+)?[a-z_][a-z0-9_.]*\s+set\b"),
                    re.compile(
                        r"\b(?:create|drop)\s+(?:table|view|dataset)\b|"
                        r"\btruncate\s+table\b"
                    ),
                    re.compile(
                        r"\b(?:erase|purge|wipe)\b.{0,24}"
                        r"\b(?:data|records?|rows?|table)\b"
                    ),
                    re.compile(
                        r"\b(?:overwrite|alter)\b.{0,24}\b(?:data|records?|rows?|table)\b"
                    ),
                    re.compile(r"\bremove\b.{0,24}\b(?:data|records?|rows?)\b"),
                ),
                "此 connector 為唯讀服務，不提供資料寫入、修改或刪除能力。",
            ),
            UnsupportedIntent(
                "external_data_source",
                "其他外部資料來源",
                (
                    re.compile(r"(?<![a-z])weather(?![a-z])|天氣|氣溫|降雨"),
                    re.compile(
                        r"(?<![a-z])(?:stock|share)\s+prices?(?![a-z])|股票|股價"
                    ),
                ),
                "目前只提供 GA4 資料，不提供天氣、股價或其他外部資料來源。",
            ),
        )

        self._explicit_ga4_pattern = re.compile(r"ga\s*4|google\s*analytics")
        ga4_lead_metric = (
            r"(?:generate[_\s-]?leads?|lead\s+(?:conversions?|events?)|"
            r"名單(?:轉換)?(?:事件|次數))"
        )
        self._explicit_ga4_lead_metric_pattern = re.compile(
            rf"{ga4_source}\s*(?:的\s*)?{ga4_lead_metric}"
        )
        external_object = (
            r"google\s*ads?|meta(?:\s*ads?)?|facebook(?:\s*ads?)?|fb\s*廣告|廣告|"
            r"seo(?:\s*(?:keyword|關鍵字)\s*(?:ranking|排名))?|search\s*console|"
            r"crm(?:\s*(?:data|records?|list|資料|名單))?|客戶關係管理|名單|銷售管線|"
            r"sales\s*pipeline|salesforce(?:\s*opportunit(?:y|ies))?|hubspot|"
            r"weather|天氣|氣溫|降雨"
        )
        self._negated_external_pattern = re.compile(
            r"^\s*(?:請\s*)?(?:(?:我|i)\s*)?"
            r"(?:不要|不用|不需要|無需|別|不是|排除|do\s+not|don['’]?t|dont)"
            r"\s*(?:(?:查|看|分析|使用)|(?:query|use|include))?\s*"
            rf"(?:{external_object})"
            r"(?:\s*(?:data|metrics?|performance|spend|cost|cpc|cpm|roas|ctr|"
            r"clicks?|impressions?|conversions?|ranking|records?|list|"
            r"資料|指標|成效|花費|費用|成本|曝光|點擊|轉換|排名|清單|列表))?"
        )
        clause_separator = (
            r"[，,。；;]+|"
            r"(?<![a-z])(?:and|but|or|plus|then|with|versus|vs\.?|to|against)"
            r"(?![a-z])|"
            r"(?<![a-z])(?:compared\s+(?:to|with)|in\s+comparison\s+(?:to|with))"
            r"(?![a-z])|"
            r"(?:以及|並且|同時|加上|然後|再查|或者|或|相較於|相較|相比於|相比|對比|(?<!參)與)|"
            r"\s+[和跟]\s+"
        )
        self._clause_separator_pattern = re.compile(clause_separator)
        self._capturing_clause_separator_pattern = re.compile(rf"({clause_separator})")
        self._trailing_clause_separator_pattern = re.compile(
            rf"(?:{clause_separator})\s*$"
        )
        self._customer_qualifier_pattern = re.compile(
            r"(?:for\s+[a-z0-9][a-z0-9 ._-]*|"
            r"(?:customer|client|account|tenant)\s*[:：]\s*[a-z0-9][a-z0-9 ._-]*|"
            r"(?:客戶|帳戶|租戶)(?:名稱)?\s*(?:是|為|[:：])\s*[\u3400-\u9fff0-9a-z ._-]+)"
        )
        self._period_qualifier_pattern = re.compile(
            r"(?:"
            r"(?:最近|過去|近|前|本|上|這|上一個)\s*(?:\d+|[一二三四五六七八九十]+)?\s*"
            r"(?:天|日|週|周|星期|個月|月|年)|"
            r"(?:今天|昨天|本週|這週|上週|本月|這個月|上個月|今年|去年)|"
            r"(?:past|last|previous|recent)\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
            r"(?:day|days|week|weeks|month|months|year|years)|"
            r"(?:today|yesterday|this\s+week|last\s+week|this\s+month|last\s+month)|"
            r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s*(?:到|至|to|through|~|－|-)\s*\d{4}[-/]\d{1,2}[-/]\d{1,2})?"
            r")"
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
                    "capability_id": "capability_lookup",
                    "data_source": "local_metadata",
                    "tools": ["get_ga4_capabilities"],
                },
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

        normalized_request = self._affirmative_request(self._normalize(request))
        if not normalized_request:
            return self._resolution(
                request=request,
                resolution="needs_clarification",
                reason_code="only_excluded_capabilities",
                message="請說明排除這些資料來源後，要查詢的 GA4 指標。",
                next_action={
                    "type": "ask_user",
                    "question": "你想改查哪個 GA4 指標？",
                },
            )
        ga4_lead_metric_match = self._explicit_ga4_lead_metric_pattern.search(
            normalized_request
        )
        is_ga4_lead_metric = ga4_lead_metric_match is not None
        ga4_ads_attribution_match = self._explicit_ga4_ads_attribution_pattern.search(
            normalized_request
        )
        is_ga4_ads_attribution = ga4_ads_attribution_match is not None
        ga4_crm_attribution_match = self._explicit_ga4_crm_attribution_pattern.search(
            normalized_request
        )
        is_ga4_crm_attribution = ga4_crm_attribution_match is not None
        is_ga4_site_search = (
            self._explicit_ga4_site_search_pattern.search(normalized_request)
            is not None
        )
        for intent in self.unsupported_intents:
            intent_request = normalized_request
            if (
                intent.intent_id == "advertising_data"
                and is_ga4_ads_attribution
                and not self._ads_exclusive_metric_pattern.search(intent_request)
            ):
                intent_request = self._explicit_ga4_ads_attribution_pattern.sub(
                    " ", intent_request
                )
            if intent.intent_id == "crm" and is_ga4_lead_metric:
                intent_request = self._explicit_ga4_lead_metric_pattern.sub(
                    " ", normalized_request
                )
            if (
                intent.intent_id == "crm"
                and is_ga4_crm_attribution
                and not self._crm_exclusive_metric_pattern.search(intent_request)
            ):
                intent_request = self._explicit_ga4_crm_attribution_pattern.sub(
                    " ", intent_request
                )
            if intent.matches(intent_request):
                return self._resolution(
                    request=request,
                    resolution="unsupported",
                    reason_code=intent.intent_id,
                    message=intent.message,
                    next_action={"type": "explain_boundary"},
                )

        unresolved_clause = self._unresolved_mixed_clause(normalized_request)
        if unresolved_clause is not None:
            return self._resolution(
                request=request,
                resolution="needs_clarification",
                reason_code="unresolved_mixed_request",
                message="部分請求無法由本機 GA4 capability metadata 確認支援。",
                next_action={
                    "type": "ask_user",
                    "question": f"請確認是否只查 GA4；無法確認的部分：{unresolved_clause}",
                },
            )

        if re.search(r"流量摘要|traffic\s+summary", normalized_request):
            return self._resolution(
                request=request,
                resolution="supported",
                reason_code="ga4_traffic_summary",
                message="可使用固定的 GA4 traffic summary 查詢。",
                next_action={"type": "call_tool", "tool": "traffic_summary"},
            )

        if self._explicit_ga4_pattern.search(normalized_request):
            catalog_query = normalized_request
            if is_ga4_lead_metric:
                catalog_query = f"{catalog_query} generate_lead"
            if is_ga4_ads_attribution:
                catalog_query = f"{catalog_query} attributed conversions source"
            if is_ga4_crm_attribution:
                catalog_query = (
                    f"{catalog_query} attributed conversions source campaign"
                )
            if is_ga4_site_search:
                catalog_query = f"{catalog_query} search_terms_count search_term"
            search_result = self.catalog.search(catalog_query, limit=10)
        else:
            search_result = {"metrics": []}
        if search_result["metrics"] and (
            is_ga4_lead_metric
            or is_ga4_ads_attribution
            or is_ga4_crm_attribution
            or is_ga4_site_search
            or self._has_catalog_match(normalized_request, search_result["metrics"])
        ):
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

    def _affirmative_request(self, request: str) -> str:
        """Remove only explicitly negated external-source spans."""

        clauses = self._capturing_clause_separator_pattern.split(request)
        without_exclusions = [
            (
                self._negated_external_pattern.sub(" ", clause, count=1)
                if not self._clause_separator_pattern.fullmatch(clause)
                else clause
            )
            for clause in clauses
        ]
        result = " ".join(without_exclusions)
        result = self._trailing_clause_separator_pattern.sub("", result)
        return result.strip(" ，,。；;")

    def _unresolved_mixed_clause(self, request: str) -> str | None:
        """Return the first affirmative mixed clause not covered by GA4 metadata."""

        if not (
            self._explicit_ga4_pattern.search(request)
            or re.search(r"流量摘要|traffic\s+summary", request)
        ):
            return None
        clauses = [
            clause.strip()
            for clause in self._clause_separator_pattern.split(request)
            if clause.strip()
        ]
        if len(clauses) < 2:
            return None

        for clause in clauses:
            if self._is_query_qualifier_clause(clause):
                continue
            if re.search(r"流量摘要|traffic\s+summary", clause):
                continue
            candidates = self.catalog.search(clause, limit=10)["metrics"]
            if not candidates or not self._has_complete_catalog_match(
                clause, candidates
            ):
                return clause
        return None

    def _has_complete_catalog_match(
        self,
        request: str,
        candidates: list[dict[str, Any]],
    ) -> bool:
        """Require every meaningful English token in a mixed clause to be known."""

        if not self._has_catalog_match(request, candidates):
            return False

        ignored_tokens = {
            "a",
            "an",
            "analysis",
            "analytics",
            "analyze",
            "breakdown",
            "by",
            "check",
            "compare",
            "data",
            "day",
            "days",
            "for",
            "from",
            "ga4",
            "get",
            "give",
            "google",
            "last",
            "me",
            "metric",
            "metrics",
            "month",
            "months",
            "of",
            "past",
            "please",
            "previous",
            "query",
            "recent",
            "report",
            "run",
            "show",
            "the",
            "this",
            "today",
            "week",
            "weeks",
            "year",
            "years",
            "yesterday",
        }
        known_ga4_values = {"direct", "natural", "organic", "paid", "referral"}
        request_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", request.casefold())
            if token not in ignored_tokens
        }
        catalog_tokens: set[str] = set()
        for candidate in candidates:
            terms = [
                candidate["metric_id"],
                candidate["label"],
                candidate["main_metric"],
                *(
                    value
                    for dimension in candidate["dimensions"]
                    for value in (dimension["dimension_id"], dimension["label"])
                ),
            ]
            for term in terms:
                catalog_tokens.update(re.findall(r"[a-z0-9]+", term.casefold()))

        if not request_tokens.issubset(catalog_tokens | known_ga4_values):
            return False

        chinese_residue = "".join(re.findall(r"[\u3400-\u9fff]+", request))
        ignored_chinese_phrases = {
            "一",
            "七",
            "三",
            "上個月",
            "上週",
            "下",
            "之",
            "二",
            "五",
            "今年",
            "今天",
            "以",
            "依",
            "六",
            "分析",
            "列出",
            "前",
            "十",
            "去年",
            "取得",
            "呈現",
            "四",
            "天",
            "年",
            "我想看",
            "我要",
            "按",
            "指標",
            "搜尋",
            "數據",
            "日",
            "明細",
            "昨天",
            "最近",
            "月",
            "本月",
            "本週",
            "查",
            "查詢",
            "查看",
            "比較",
            "每個月",
            "每日",
            "每週",
            "的",
            "請",
            "給我",
            "資料",
            "趨勢",
            "近",
            "過去",
            "顯示",
        }
        known_chinese_values = {
            "付費",
            "使用者",
            "來源",
            "媒介",
            "工作階段",
            "收益",
            "活動",
            "推薦流量",
            "直接流量",
            "自然流量",
            "裝置",
            "轉換",
        }
        catalog_chinese_phrases = {
            phrase
            for candidate in candidates
            for term in (
                candidate["label"],
                *(dimension["label"] for dimension in candidate["dimensions"]),
            )
            for phrase in re.findall(r"[\u3400-\u9fff]+", term)
        }
        for phrase in sorted(
            ignored_chinese_phrases | known_chinese_values | catalog_chinese_phrases,
            key=len,
            reverse=True,
        ):
            chinese_residue = chinese_residue.replace(phrase, "")
        return not chinese_residue

    def _is_query_qualifier_clause(self, clause: str) -> bool:
        """Recognize customer and period context, not a second analysis request."""

        normalized = clause.strip()
        return bool(
            self._customer_qualifier_pattern.fullmatch(normalized)
            or self._period_qualifier_pattern.fullmatch(normalized)
        )

    def _has_catalog_match(
        self,
        request: str,
        candidates: list[dict[str, Any]],
    ) -> bool:
        """Require catalog-derived evidence beyond a fuzzy single-token hit."""

        request_compact = self._compact(request)
        request_tokens = set(re.findall(r"[a-z0-9]+", request.casefold()))
        request_signal_tokens = request_tokens - {
            "analyze",
            "analytics",
            "breakdown",
            "by",
            "check",
            "display",
            "fetch",
            "find",
            "ga4",
            "get",
            "give",
            "google",
            "list",
            "me",
            "of",
            "please",
            "query",
            "report",
            "retrieve",
            "show",
            "the",
            "view",
        }
        request_chinese_terms = re.findall(r"[\u3400-\u9fff]{2,}", request)
        for candidate in candidates:
            terms = [
                candidate["metric_id"],
                candidate["label"],
                candidate["main_metric"],
                *(
                    value
                    for dimension in candidate["dimensions"]
                    for value in (dimension["dimension_id"], dimension["label"])
                ),
            ]
            for term in terms:
                compact_term = self._compact(term)
                if len(compact_term) >= 2 and compact_term in request_compact:
                    return True
                if any(
                    chinese_term in compact_term
                    for chinese_term in request_chinese_terms
                ):
                    return True

                term_tokens = [
                    token
                    for token in re.findall(r"[a-z0-9]+", term.casefold())
                    if token not in {"by", "count"} and len(token) >= 3
                ]
                if len(term_tokens) >= 2 and set(term_tokens).issubset(request_tokens):
                    return True

            for dimension in candidate["dimensions"]:
                dimension_tokens = {
                    token
                    for token in re.findall(
                        r"[a-z0-9]+", dimension["dimension_id"].casefold()
                    )
                    if token
                    not in {
                        "category",
                        "event",
                        "first",
                        "flow",
                        "label",
                        "page",
                        "session",
                        "traffic",
                    }
                    and len(token) >= 3
                }
                if request_signal_tokens and request_signal_tokens.issubset(
                    dimension_tokens
                ):
                    return True
        return False

    @staticmethod
    def _compact(value: str) -> str:
        return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.casefold())

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
