from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import Any


CATALOG_PATH = Path(__file__).parent / "semantic" / "catalog.v1.json"
SUPPORTED_PROFILES = ("non_ecommerce", "ecommerce")
PROFILE_ALIASES = {
    "non_ecommerce": "non_ecommerce",
    "non-ecommerce": "non_ecommerce",
    "非電商": "non_ecommerce",
    "ecommerce": "ecommerce",
    "e-commerce": "ecommerce",
    "電商": "ecommerce",
}
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
TABLE_REFERENCE_PATTERN = re.compile(
    r"`ga4_bq_id\.ga4_mar\.([A-Za-z0-9_-]+)`"
)
ALL_BACKTICK_REFERENCE_PATTERN = re.compile(r"`([^`]+)`")
FORBIDDEN_SQL_PATTERN = re.compile(
    r"(?i)\b(?:ALTER|CALL|CREATE|DELETE|DROP|EXECUTE|EXPORT|GRANT|INSERT|MERGE|REVOKE|TRUNCATE|UPDATE)\b"
)


class SemanticCatalogError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


def normalize_profile(profile: str | None) -> str | None:
    if profile is None or not profile.strip():
        return None
    normalized = PROFILE_ALIASES.get(profile.strip().casefold())
    if normalized is None:
        raise SemanticCatalogError(
            "invalid_semantic_profile",
            "semantic profile 必須是 ecommerce（電商）或 non_ecommerce（非電商）。",
            details={"supported_profiles": list(SUPPORTED_PROFILES)},
        )
    return normalized


def _normalize_search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _compact_search_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", _normalize_search_text(value))


def _semantic_definition(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metric.get(key)
        for key in (
            "category",
            "model",
            "type_params",
            "filter",
            "dimensions",
            "group_by_sql",
        )
    }


def _approved_table_references(sql_template: str) -> set[str]:
    sql = sql_template.strip()
    sql_without_trailing_semicolon = sql.removesuffix(";").rstrip()
    if not re.match(r"(?is)^(?:SELECT|WITH)\b", sql_without_trailing_semicolon):
        raise ValueError("Semantic SQL must start with SELECT or WITH")
    if ";" in sql_without_trailing_semicolon:
        raise ValueError("Semantic SQL must contain exactly one statement")
    if FORBIDDEN_SQL_PATTERN.search(sql_without_trailing_semicolon):
        raise ValueError("Semantic SQL contains a forbidden statement keyword")

    approved_tables = set(TABLE_REFERENCE_PATTERN.findall(sql_without_trailing_semicolon))
    all_references = set(ALL_BACKTICK_REFERENCE_PATTERN.findall(sql_without_trailing_semicolon))
    expected_references = {
        f"ga4_bq_id.ga4_mar.{table_name}"
        for table_name in approved_tables
    }
    if not approved_tables or all_references != expected_references:
        raise ValueError("Semantic SQL contains an unapproved table reference")
    return approved_tables


class SemanticCatalog:
    def __init__(self, catalog: dict[str, Any]):
        self.catalog = catalog
        self.version = str(catalog["catalog_version"])
        self.models = catalog["models"]
        self.dimensions = catalog["dimensions"]
        self.profiles = catalog["profiles"]
        self._validate()

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> "SemanticCatalog":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def _validate(self) -> None:
        if set(self.profiles) != set(SUPPORTED_PROFILES):
            raise ValueError("Semantic catalog profiles are incomplete")
        for profile, profile_data in self.profiles.items():
            for metric_id, metric in profile_data["metrics"].items():
                if metric["metric_id"] != metric_id:
                    raise ValueError(f"Metric key mismatch: {profile}/{metric_id}")
                if metric["status"] not in {"published", "conflict"}:
                    raise ValueError(f"Invalid metric status: {profile}/{metric_id}")
                if metric["status"] != "published":
                    continue
                if metric["model"] not in self.models:
                    raise ValueError(f"Unknown model: {profile}/{metric_id}")
                unknown_dimensions = set(metric["dimensions"]) - set(self.dimensions)
                if unknown_dimensions:
                    raise ValueError(
                        f"Unknown dimensions for {profile}/{metric_id}: {unknown_dimensions}"
                    )
                try:
                    table_references = _approved_table_references(
                        metric["sql_template"]
                    )
                except ValueError as error:
                    raise ValueError(
                        f"Invalid SQL template: {profile}/{metric_id}: {error}"
                    ) from error
                if not table_references.issubset(self.models):
                    raise ValueError(
                        f"Unapproved table reference: {profile}/{metric_id}"
                    )
                has_start_date = "DATE 'start_date'" in metric["sql_template"]
                has_end_date = "DATE 'end_date'" in metric["sql_template"]
                if has_start_date != has_end_date:
                    raise ValueError(
                        f"Incomplete date placeholders: {profile}/{metric_id}"
                    )

    def _profile_metrics(self, profile: str) -> dict[str, dict[str, Any]]:
        return self.profiles[profile]["metrics"]

    def get_metric(self, profile: str, metric_id: str) -> dict[str, Any]:
        metric = self._profile_metrics(profile).get(metric_id)
        if metric is None:
            raise SemanticCatalogError(
                "unsupported_metric",
                f"semantic catalog 的 {profile} profile 不支援指標「{metric_id}」。",
                details={"metric_id": metric_id, "profile": profile},
            )
        if metric["status"] != "published":
            raise SemanticCatalogError(
                "metric_definition_conflict",
                f"指標「{metric_id}」存在多個定義，尚未發布查詢。",
                details={"metric_id": metric_id, "profile": profile},
            )
        return metric

    def resolve_profile(
        self,
        metric_ids: list[str],
        requested_profile: str | None,
    ) -> tuple[str, str]:
        profile = normalize_profile(requested_profile)
        if profile is not None:
            for metric_id in metric_ids:
                self.get_metric(profile, metric_id)
            return profile, "explicit"

        candidates = []
        for candidate in SUPPORTED_PROFILES:
            metrics = self._profile_metrics(candidate)
            if all(
                metric_id in metrics and metrics[metric_id]["status"] == "published"
                for metric_id in metric_ids
            ):
                candidates.append(candidate)

        if not candidates:
            known_profiles = {
                metric_id: [
                    candidate
                    for candidate in SUPPORTED_PROFILES
                    if metric_id in self._profile_metrics(candidate)
                ]
                for metric_id in metric_ids
            }
            raise SemanticCatalogError(
                "unsupported_metric",
                "要求的指標組合沒有可用的 semantic profile。",
                details={"metric_profiles": known_profiles},
            )
        if len(candidates) == 1:
            return candidates[0], "metric_availability"

        definitions_match = all(
            _semantic_definition(self.get_metric(candidates[0], metric_id))
            == _semantic_definition(self.get_metric(candidates[1], metric_id))
            for metric_id in metric_ids
        )
        if definitions_match:
            return candidates[0], "shared_definition"

        raise SemanticCatalogError(
            "semantic_profile_required",
            "這些指標在電商與非電商有不同定義，請先確認客戶的網站類型。",
            details={
                "metric_ids": metric_ids,
                "supported_profiles": list(SUPPORTED_PROFILES),
            },
        )

    def search(
        self,
        query: str,
        profile: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        normalized_profile = normalize_profile(profile)
        limit = max(1, min(int(limit), 25))
        query_normalized = _normalize_search_text(query)
        query_compact = _compact_search_text(query)
        query_tokens = [
            token
            for token in re.split(r"[\s,，、/]+", query_normalized)
            if token
        ]

        result_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        profiles = [normalized_profile] if normalized_profile else list(SUPPORTED_PROFILES)
        for candidate_profile in profiles:
            assert candidate_profile is not None
            for metric_id, metric in self._profile_metrics(candidate_profile).items():
                if metric["status"] != "published":
                    continue
                dimension_labels = [
                    self.dimensions[dimension_id]["label"]
                    for dimension_id in metric["dimensions"]
                ]
                contexts = metric.get("contexts", [])
                searchable_parts = [
                    metric_id,
                    metric["label"],
                    metric["main_metric"],
                    *metric["dimensions"],
                    *dimension_labels,
                ]
                for context in contexts:
                    searchable_parts.extend(context.values())
                searchable = " ".join(part for part in searchable_parts if part)
                searchable_normalized = _normalize_search_text(searchable)
                searchable_compact = _compact_search_text(searchable)

                if not query_compact:
                    score = 1
                else:
                    score = 0
                    if query_compact in searchable_compact:
                        score += 30
                    if _compact_search_text(metric["label"]) in query_compact:
                        score += 20
                    if _compact_search_text(metric_id) in query_compact:
                        score += 20
                    score += sum(
                        5 for token in query_tokens if token in searchable_normalized
                    )
                    for term in (
                        metric["label"],
                        *dimension_labels,
                    ):
                        compact_term = _compact_search_text(term)
                        if compact_term and compact_term in query_compact:
                            score += 8
                if score <= 0:
                    continue

                definition_key = json.dumps(
                    _semantic_definition(metric),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                result_key = (metric_id, definition_key)
                existing = result_by_key.get(result_key)
                if existing is None:
                    existing = {
                        "metric_id": metric_id,
                        "label": metric["label"],
                        "main_metric": metric["main_metric"],
                        "category": metric["category"],
                        "dimensions": [
                            {
                                "dimension_id": dimension_id,
                                "label": self.dimensions[dimension_id]["label"],
                            }
                            for dimension_id in metric["dimensions"]
                        ],
                        "date_scope": (
                            "requested_period"
                            if "DATE 'start_date'" in metric["sql_template"]
                            else "all_available_data"
                        ),
                        "profiles": [],
                        "score": score,
                    }
                    result_by_key[result_key] = existing
                existing["profiles"].append(candidate_profile)
                existing["score"] = max(existing["score"], score)

        results = sorted(
            result_by_key.values(),
            key=lambda item: (-item["score"], item["metric_id"]),
        )[:limit]
        for result in results:
            result.pop("score", None)
        return {
            "status": "ok",
            "catalog_version": self.version,
            "profile": normalized_profile,
            "query": query,
            "count": len(results),
            "metrics": results,
        }

    def compile_sql(
        self,
        profile: str,
        metric_id: str,
        project_id: str,
        dataset_id: str,
        result_limit: int,
    ) -> tuple[str, dict[str, Any]]:
        if not IDENTIFIER_PATTERN.fullmatch(project_id):
            raise SemanticCatalogError(
                "invalid_tenant_routing",
                "Registry project_id 格式不合法。",
            )
        if not IDENTIFIER_PATTERN.fullmatch(dataset_id):
            raise SemanticCatalogError(
                "invalid_tenant_routing",
                "Registry dataset_id 格式不合法。",
            )
        metric = self.get_metric(profile, metric_id)
        sql_template = metric["sql_template"]
        try:
            table_references = _approved_table_references(sql_template)
        except ValueError as error:
            raise SemanticCatalogError(
                "invalid_metric_definition",
                f"指標「{metric_id}」的 SQL template 未通過安全驗證。",
            ) from error
        if not table_references.issubset(self.models):
            raise SemanticCatalogError(
                "invalid_metric_definition",
                f"指標「{metric_id}」參照了未核准的資料表。",
            )

        sql = sql_template.replace(
            "`ga4_bq_id.ga4_mar.",
            f"`{project_id}.{dataset_id}.",
        )
        sql = sql.replace("DATE 'start_date'", "@start_date")
        sql = sql.replace("DATE 'end_date'", "@end_date")
        sql = sql.rstrip().removesuffix(";").rstrip()
        if "ga4_bq_id" in sql or "DATE 'start_date'" in sql or "DATE 'end_date'" in sql:
            raise SemanticCatalogError(
                "invalid_metric_definition",
                f"指標「{metric_id}」仍含有未解析的 SQL placeholder。",
            )

        result_limit = max(1, min(int(result_limit), 200))
        wrapped_sql = (
            "SELECT *\n"
            "FROM (\n"
            f"{sql}\n"
            ") AS semantic_result\n"
            f"LIMIT {result_limit + 1}"
        )
        return wrapped_sql, metric


semantic_catalog = SemanticCatalog.load()
