"""Phase 11 v1 wire contract. No collection, I/O, or request hooks.

Only construct these records from trusted, explicitly selected context fields.
Validation is NOT redaction: free text must pass the separate summary sanitizer.
Never log a ValidationError (it can contain the rejected input).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"
CANONICAL_RETENTION_DAYS = 180
SUMMARY_RETENTION_DAYS = 30
MAX_SUMMARY_CHARACTERS = 500

Transport = Literal["mcp", "rest"]
RequestKind = Literal["analytics", "capability_preflight", "discovery", "unclassified"]
Host = Literal["chatgpt", "claude", "web_app", "other"]
Goal = Literal[
    "overview", "comparison", "trend", "breakdown", "ranking", "diagnosis",
    "recommendation", "data_lookup", "unknown",
]
Subject = Literal[
    "traffic", "acquisition", "campaign", "content", "landing_page", "audience",
    "conversion", "engagement", "cross_source", "unknown",
]
IntentSource = Literal[
    "server_rule", "host_model_hint", "offline_classifier", "manual_review", "unknown",
]
SummarySource = Literal[
    "host_model_generated", "server_generated", "client_generated", "user_input",
]
ToolName = Literal[
    "customer_lookup", "list_available_customers", "get_ga4_capabilities",
    "search_ga4_metrics", "query_ga4", "traffic_summary",
]
PeriodType = Literal["explicit_range", "relative_window", "multiple_periods", "unknown"]
ComparisonType = Literal["none", "previous_period", "explicit_periods", "unknown"]
ErrorCode = Literal[
    "authentication_required", "invalid_token", "insufficient_scope", "tenant_access_denied",
    "invalid_schema", "unknown_tool", "invalid_customer_name", "customer_name_too_broad",
    "tenant_confirmation_required", "ambiguous_tenant", "tenant_not_found", "tenant_inactive",
    "data_unavailable", "invalid_date_format", "invalid_date_range", "invalid_period",
    "date_before_available_range", "date_range_too_large", "future_date_not_allowed",
    "query_cost_estimate_failed", "query_cost_limit_exceeded", "daily_query_quota_exceeded",
    "invalid_semantic_profile", "unsupported_metric", "metric_definition_conflict",
    "semantic_profile_required", "invalid_tenant_routing", "invalid_metric_definition",
    "invalid_metric_request", "too_many_metrics", "invalid_report_contract",
    "timeout", "backend_error", "unknown_error",
]
UnsupportedReason = Literal[
    "external_source_not_available", "unsupported_metric", "metric_definition_conflict",
    "period_not_supported", "unknown",
]
Code = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")]
OpaqueTenantId = Annotated[str, Field(max_length=256)]
MetricIds = Annotated[list[Code], Field(max_length=100)]
NonNegativeInt = Annotated[int, Field(ge=0, le=2**63 - 1)]
PositiveInt = Annotated[int, Field(gt=0, le=2**63 - 1)]


class WireRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_time: datetime
    interaction_id: Annotated[str, Field(
        min_length=36, max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )]

    @field_validator("event_time")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("event_time requires a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("interaction_id")
    @classmethod
    def uuid_text(cls, value: str) -> str:
        if len(value) != 36 or str(UUID(value)) != value:
            raise ValueError("interaction_id requires canonical UUID text")
        return value

    def wire_dict(self) -> dict:
        # Revalidation also guards mutated lists and model_copy(update=...) bypasses.
        return type(self).model_validate(self.model_dump(warnings=False)).model_dump(mode="json")


class UsageEvent(WireRecord):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [{
            "if": {"properties": {"identity_status": {"const": "verified"}},
                   "required": ["identity_status"]},
            "then": {"properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
            "else": {"properties": {"user_id": {"type": "null"}}},
        }],
    })
    event_name: Literal["analytics_request_completed"] = "analytics_request_completed"
    transport: Transport
    request_kind: RequestKind
    user_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    identity_status: Literal["verified", "unavailable"] = "unavailable"
    host: Host = "other"
    tenant_id: OpaqueTenantId | None = None
    authorization_scope_ref: Code | None = None
    authorization_result: Literal["allowed", "denied", "unknown"] = "unknown"
    tool_name: ToolName | None = None
    request_summary: None = None
    request_summary_source: Literal["unavailable"] = "unavailable"
    analysis_goal: Goal = "unknown"
    analysis_subject: Subject = "unknown"
    intent_source: IntentSource = "unknown"
    intent_taxonomy_version: Literal["v1"] = "v1"
    metrics: MetricIds = Field(default_factory=list)
    dimensions: MetricIds = Field(default_factory=list)
    period_type: PeriodType = "unknown"
    requested_days: PositiveInt | None = None
    comparison_type: ComparisonType = "unknown"
    resolution: Literal["supported", "needs_clarification", "unsupported", "unknown"] = "unknown"
    status: Literal["success", "failure", "denied", "unsupported", "needs_clarification"]
    latency_ms: NonNegativeInt
    result_row_count: NonNegativeInt | None = None
    error_code: ErrorCode | None = None
    unsupported_reason: UnsupportedReason | None = None

    @field_validator("metrics", "dimensions")
    @classmethod
    def distinct_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def identity_consistency(self) -> UsageEvent:
        if (self.user_id is not None) != (self.identity_status == "verified"):
            raise ValueError("identity_status and pseudonymous user_id disagree")
        return self


class SummaryAttachment(WireRecord):
    """Accept only already sanitized text, never raw requests or tool results."""

    event_name: Literal["analytics_request_summary"] = "analytics_request_summary"
    request_summary: Annotated[str, Field(
        min_length=1, max_length=MAX_SUMMARY_CHARACTERS, pattern=r"\S",
    )]
    request_summary_source: SummarySource

    @field_validator("request_summary")
    @classmethod
    def nonblank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must contain text")
        return value


def tenant_id_for_event(value: object) -> tuple[str | None, str | None]:
    """Return opaque ID and payload-free quality code; never coerce or normalize."""
    if value is None:
        return None, "tenant_id_missing"
    if not isinstance(value, str):
        return None, "tenant_id_type_invalid"
    if len(value) > 256:
        return None, "tenant_id_too_long"
    return value, None


def bigquery_payload_schema(model: type[WireRecord]) -> list[dict[str, str]]:
    """Logical payload schema, not the Cloud Logging export envelope/table schema.

    Routing must preserve Logging's timestamp/jsonPayload envelope and field-name
    transformations. Nullable-only summary fields still need explicit STRING types.
    """
    properties = model.model_json_schema()["properties"]
    fields = []
    for name, spec in properties.items():
        options = spec.get("anyOf", [spec])
        concrete = next((part for part in options if part.get("type") != "null"), {})
        kind = concrete.get("type")
        field_type = {"integer": "INT64", "array": "STRING"}.get(kind, "STRING")
        if name == "event_time":
            field_type = "TIMESTAMP"
        nullable = any(part.get("type") == "null" for part in options)
        mode = "REPEATED" if kind == "array" else "NULLABLE" if nullable else "REQUIRED"
        fields.append({"name": name, "type": field_type, "mode": mode})
    return fields


def wire_json_schema(model: type[WireRecord]) -> dict:
    """Serialized records include every field, even nullable/defaulted fields."""
    schema = model.model_json_schema(mode="serialization")
    schema["required"] = list(schema["properties"])
    return schema
