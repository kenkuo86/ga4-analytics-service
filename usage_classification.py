"""Pure, bounded v1 classification from metadata selected by trusted adapters.

No BigQuery calls, raw-request logging, SQL parsing or cross-tool inference.
"""
from datetime import date
import re
from typing import get_args
import unicodedata

from semantic_catalog import SemanticCatalogError, semantic_catalog
from traffic_summary_report import TRAFFIC_METRICS
from usage_contract import Goal, Subject

_EXTERNAL_REASONS = frozenset({"advertising_data", "seo_keyword_ranking", "crm", "external_data_source"})
_MAIN_SUBJECTS = {
    "sessions": "traffic", "users": "traffic", "traffic_flow_weight": "traffic",
    "count_page_view": "content", "avg_page_view": "content", "avg_user_page_view": "content",
    "search_terms_count": "content", "avg_session_duration": "engagement",
    "bounce_rate": "engagement", "bounces": "engagement", "engagement": "engagement",
    "count_user_engagements": "engagement", "count_events": "engagement",
    "count_generate_leads": "conversion", "conversions": "conversion", "conversion_rate": "conversion",
    "converstion_rate": "conversion", "revenue": "conversion", "count_purchase": "conversion",
    "aov": "conversion", "attributed_conversions": "conversion", "attributed_revenue": "conversion",
    "attributed_conversion_rate": "conversion", "customers": "audience", "customer_ratio": "audience",
    "churn_rate": "audience",
}
_DIMENSION_SUBJECTS = (
    (frozenset({"first_page_title"}), "landing_page"),
    (frozenset({"session_campaign"}), "campaign"),
    (frozenset({"session_source", "session_medium", "session_source_medium", "session_default_channel_group"}), "acquisition"),
    (frozenset({"page_title", "page_location_clean", "search_term"}), "content"),
    (frozenset({"country", "region", "device_category"}), "audience"),
)


def explicit_period(start: date | None, end: date | None) -> dict:
    """Dates must already be parsed by the service's normal date-validation path."""
    if type(start) is not date or type(end) is not date or end < start:
        return {"period_type": "unknown", "requested_days": None}
    return {"period_type": "explicit_range", "requested_days": (end - start).days + 1}


def preflight_period(period: dict | None) -> dict:
    result = {"period_type": "unknown", "requested_days": None, "comparison_type": "unknown"}
    if not isinstance(period, dict) or period.get("outcome") != "resolved":
        return result
    intervals = period.get("explicit_periods", [])
    days = period.get("requested_days")
    if not intervals or type(days) is not int or days <= 0:
        return result
    # Reuse Phase 10's union count, including reliable over-limit demand.
    result["requested_days"] = days
    if len(intervals) > 1:
        result["period_type"] = "multiple_periods"
    elif intervals[0].get("window_kind") == "explicit_date":
        result["period_type"] = "explicit_range"
    else:
        result["period_type"] = "relative_window"
    if len(intervals) > 1 and period.get("comparison_modifier") is True:
        result["comparison_type"] = "explicit_periods"
    elif period.get("implicit_periods"):
        result["comparison_type"] = "previous_period"
    return result


def _metric_subject(metric: dict) -> str:
    dimensions = set(metric.get("dimensions", []))
    for ids, subject in _DIMENSION_SUBJECTS:
        if ids & dimensions:
            return subject
    return _MAIN_SUBJECTS.get(metric.get("main_metric"), "unknown")


def classify(
    tool_name: str, *, resolved_metric_ids=(), resolved_profile=None,
    capability: dict | None = None, parsed_start=None, parsed_end=None,
    goal_hint=None, subject_hint=None,
) -> dict:
    """Read only local metadata; callers supply actual resolved IDs, not candidates."""
    result = {
        "analysis_goal": "unknown", "analysis_subject": "unknown", "intent_source": "unknown",
        "metrics": [], "dimensions": [], "comparison_type": "unknown",
        "period_type": "unknown", "requested_days": None,
    }
    if tool_name == "traffic_summary":
        result.update(analysis_subject="traffic", metrics=[m["metric_id"] for m in TRAFFIC_METRICS],
                      dimensions=["session_date"], comparison_type="previous_period")
        result.update(explicit_period(parsed_start, parsed_end))
    elif tool_name == "query_ga4":
        result.update(explicit_period(parsed_start, parsed_end))
        subjects = set()
        # Bounds before iteration; no arbitrary caller strings can become metric IDs.
        if isinstance(resolved_metric_ids, (tuple, list)) and len(resolved_metric_ids) <= 5 and isinstance(resolved_profile, str) and resolved_profile in semantic_catalog.profiles:
            for metric_id in resolved_metric_ids:
                if not isinstance(metric_id, str) or len(metric_id) > 128:
                    continue
                try:
                    metric = semantic_catalog.get_metric(resolved_profile, metric_id)
                except SemanticCatalogError:
                    continue
                result["metrics"].append(metric_id)
                result["dimensions"].extend(d for d in metric["dimensions"] if d in semantic_catalog.dimensions)
                subjects.add(_metric_subject(metric))
            if len(subjects) == 1:
                result["analysis_subject"] = subjects.pop()
        result["metrics"] = list(dict.fromkeys(result["metrics"]))
        result["dimensions"] = list(dict.fromkeys(result["dimensions"]))
    elif tool_name == "get_ga4_capabilities" and isinstance(capability, dict):
        result.update(preflight_period(capability.get("period")))
        if capability.get("reason_code") in _EXTERNAL_REASONS:
            result["analysis_subject"] = "cross_source"
        elif capability.get("reason_code") == "ga4_traffic_summary":
            result["analysis_subject"] = "traffic"
        period = capability.get("period")
        if isinstance(period, dict) and period.get("comparison_modifier") is True:
            result["analysis_goal"] = "comparison"
        # Candidates are not executed metrics/dimensions and remain empty arrays.
    hint_used = False
    for key, value, values in (("analysis_goal", goal_hint, get_args(Goal)),
                               ("analysis_subject", subject_hint, get_args(Subject))):
        if key == "analysis_subject" and value == "cross_source" and (tool_name in ("traffic_summary", "query_ga4") or (isinstance(capability, dict) and capability.get("resolution") == "supported")):
            continue
        if result[key] == "unknown" and isinstance(value, str) and value in values and value != "unknown":
            result[key] = value
            hint_used = True
    if hint_used:
        result["intent_source"] = "host_model_hint"
    elif result["analysis_goal"] != "unknown" or result["analysis_subject"] != "unknown":
        result["intent_source"] = "server_rule"
    return result


# Conservative vocabulary, not a general PII detector. Unknown prose is discarded.
# Host summaries can use these short analytical phrases; safe server summaries use
# only tool names, validated dates, and validated catalog IDs, never customer names.
_SUMMARY_WORDS = (
    "GA4", "ga4", "查詢", "分析", "比較", "流量摘要", "流量", "趨勢", "來源", "媒介", "活動",
    "內容", "到達頁面", "使用者", "工作階段", "轉換", "互動", "指標", "維度", "排名", "診斷",
    "建議", "需求", "不支援", "未知", "日期", "期間", "本期", "前期", "上週", "本週", "上月",
    "本月", "最近", "過去", "天", "日", "週", "月", "年", "與", "至", "的", "請", "幫我",
    "sessions", "users", "traffic", "summary", "compare", "trend", "[redacted]",
)
_SUMMARY_TOKEN = re.compile(r"(?:" + "|".join(re.escape(s) for s in sorted(_SUMMARY_WORDS, key=len, reverse=True)) + r"|(?<![A-Za-z0-9])\d{4}-\d{2}-\d{2}(?!\d)|(?<![A-Za-z0-9])\d{1,3}(?!\d)|[\s，。、：；,.:;()（）/\-])")
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![A-Za-z0-9])(?:\+?\d[\d ()./\-]{6,}\d)(?!\d)")
_DANGER = re.compile(r"(?i)authorization|bearer|cookie|secret|password|credential|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|private[_ -]?key|-----BEGIN|\bSELECT\b|\bWITH\b|https?://|eyJ[A-Za-z0-9_-]+\.")


def sanitize_summary(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    value = unicodedata.normalize("NFKC", value)
    if _DANGER.search(value):
        return None
    value = _EMAIL.sub("[redacted]", value)
    # Protect ISO dates from the conservative phone detector.
    dates = []
    def protect(match):
        dates.append(match.group())
        return "DATEPLACEHOLDER" + chr(0xE000 + len(dates) - 1)
    value = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", protect, value)
    value = _PHONE.sub("[redacted]", value)
    for i, text in enumerate(dates):
        value = value.replace("DATEPLACEHOLDER" + chr(0xE000 + i), text)
    position = 0
    while position < len(value):
        match = _SUMMARY_TOKEN.match(value, position)
        if match is None:
            return None
        position = match.end()
    value = value.strip()[:500]
    return value or None


def summary_text(raw: object, *, source: str, tool_name: str, classification: dict) -> tuple[str | None, str]:
    if raw is not None:
        cleaned = sanitize_summary(raw)
        return (cleaned, source) if cleaned and source in ("host_model_generated", "client_generated", "user_input") else (None, "unavailable")
    # Only generate a minimal description when safe structured evidence exists.
    if tool_name not in ("traffic_summary", "query_ga4") or not classification.get("metrics"):
        return None, "unavailable"
    label = "流量摘要" if tool_name == "traffic_summary" else "指標"
    days = classification.get("requested_days")
    period = f"，需求期間 {days} 天" if type(days) is int and days > 0 else ""
    return f"查詢 GA4 {label}{period}", "server_generated"
