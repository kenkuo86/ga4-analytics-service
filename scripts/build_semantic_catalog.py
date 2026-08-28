from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any


EXPECTED_HEADERS = [
    "報表類型",
    "頁面名稱",
    "圖表名稱",
    "指標名稱",
    "metric_id",
    "main_metric",
    "類別",
    "資料表",
    "type_params",
    "filter",
    "group by",
    "order by",
    "sql_code",
    "sql_generation_note",
]

PROFILE_NAMES = {
    "非電商": "non_ecommerce",
    "電商": "ecommerce",
}

MODEL_DEFINITIONS = {
    "mar_ga_sessions": {
        "grain": "session",
        "default_time_dimension": "session_date",
    },
    "mar_ga_events": {
        "grain": "event",
        "default_time_dimension": "event_date",
    },
    "mar_ga_users": {
        "grain": "user",
        "default_time_dimension": None,
    },
    "mar_ga_traffic_flows": {
        "grain": "traffic_flow_edge",
        "default_time_dimension": "session_date",
    },
}

DIMENSION_DEFINITIONS = {
    "session_date": {"label": "日期", "type": "date"},
    "event_date": {"label": "日期", "type": "date"},
    "session_source": {"label": "來源", "type": "string"},
    "session_medium": {"label": "媒介", "type": "string"},
    "session_campaign": {"label": "活動", "type": "string"},
    "device_category": {"label": "裝置類別", "type": "string"},
    "country": {"label": "國家", "type": "string"},
    "region": {"label": "地區", "type": "string"},
    "user_label": {"label": "新舊使用者", "type": "string"},
    "page_title": {"label": "頁面標題", "type": "string"},
    "page_location_clean": {"label": "頁面網址", "type": "string"},
    "first_page_title": {"label": "到達頁面", "type": "string"},
    "search_term": {"label": "站內搜尋字詞", "type": "string"},
    "event_hour": {"label": "小時", "type": "integer"},
    "event_day_of_week": {"label": "星期", "type": "integer"},
    "traffic_flow_source": {"label": "流量路徑來源", "type": "string"},
    "traffic_flow_target": {"label": "流量路徑目標", "type": "string"},
    "item_name": {"label": "商品名稱", "type": "string"},
    "order_label": {"label": "訂單類型", "type": "string"},
    "revenue_range": {"label": "收益區間", "type": "string"},
    "purchase_count": {"label": "購買次數", "type": "integer"},
}

GROUP_BY_DIMENSIONS = {
    "session_date": ["session_date"],
    "event_date": ["event_date"],
    "session_source": ["session_source"],
    "session_source, session_medium": ["session_source", "session_medium"],
    "session_campaign": ["session_campaign"],
    "device_category": ["device_category"],
    "country, region": ["country", "region"],
    "user_label": ["user_label"],
    "page_title": ["page_title"],
    "first_page_title": ["first_page_title"],
    "search_term": ["search_term"],
    "extract(HOUR from event_timestamp)": ["event_hour"],
    "extract(DAYOFWEEK from event_timestamp)": ["event_day_of_week"],
    "extract(HOUR from event_timestamp), extract(DAYOFWEEK from event_timestamp)": [
        "event_hour",
        "event_day_of_week",
    ],
    "source, target": ["traffic_flow_source", "traffic_flow_target"],
    "items.item_name": ["item_name"],
    "order_label": ["order_label"],
    "purchase_count": ["purchase_count"],
}

ALLOWED_AGGREGATIONS = {
    "average": "average",
    "avg": "average",
    "count": "count",
    "count distinct": "count_distinct",
    "count_distinct": "count_distinct",
    "min": "min",
    "sum": "sum",
}
FORBIDDEN_SQL_PATTERN = re.compile(
    r"(?i)\b(?:ALTER|CALL|CREATE|DELETE|DROP|EXECUTE|EXPORT|GRANT|INSERT|MERGE|REVOKE|TRUNCATE|UPDATE)\b"
)

CANONICAL_DEFINITION_POLICIES = {
    ("non_ecommerce", "total_users"): {
        "selector": {
            "model": "mar_ga_sessions",
            "type_params": {
                "agg": "count_distinct",
                "expr": "user_pseudo_id",
                "agg_time_dimension": "session_date",
            },
        },
        "resolution": (
            "Canonical policy: total_users uses mar_ga_sessions and session_date."
        ),
    },
}


class CatalogBuildError(ValueError):
    pass


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_type_params(raw: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for line in raw.splitlines():
        match = re.match(
            r"\s*([A-Za-z_][A-Za-z0-9_ ]*)\s*(?:\(required\))?\s*:\s*(.*)$",
            line,
        )
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key == "input_metrics":
            parsed[key] = [
                item.strip()
                for item in value.strip("[]").split(",")
                if item.strip()
            ]
        else:
            parsed[key] = value

    aggregation = str(parsed.get("agg", "")).strip()
    expression = str(parsed.get("expr", "")).strip()
    normalized_agg = ALLOWED_AGGREGATIONS.get(aggregation.casefold())
    expression_as_agg = ALLOWED_AGGREGATIONS.get(expression.casefold())
    if normalized_agg is None and expression_as_agg is not None:
        aggregation, expression = expression, aggregation
        normalized_agg = expression_as_agg

    if aggregation:
        parsed["agg"] = normalized_agg or aggregation.casefold()
    if expression:
        parsed["expr"] = expression
    return parsed


def _normalize_filter(raw: str) -> str:
    normalized = re.sub(
        r"(?i)\b([A-Za-z_][A-Za-z0-9_.]*)\s*<>\s*null\b",
        r"\1 IS NOT NULL",
        raw.strip(),
    )
    return normalized


def _dimension_ids(group_by: str) -> list[str]:
    compact = group_by.strip()
    if not compact:
        return []
    if compact in GROUP_BY_DIMENSIONS:
        return GROUP_BY_DIMENSIONS[compact]
    if "page_location" in compact and compact.startswith("page_title,"):
        return ["page_title", "page_location_clean"]
    if "purchase_revenue BETWEEN" in compact:
        return ["revenue_range"]
    raise CatalogBuildError(f"Unsupported group by expression: {compact}")


def _validate_sql_template(metric_id: str, sql_template: str) -> None:
    sql = sql_template.strip().removesuffix(";").rstrip()
    if not re.match(r"(?is)^(?:SELECT|WITH)\b", sql):
        raise CatalogBuildError(f"{metric_id} SQL must start with SELECT or WITH")
    if ";" in sql or FORBIDDEN_SQL_PATTERN.search(sql):
        raise CatalogBuildError(f"{metric_id} SQL contains an unsafe statement")
    references = set(re.findall(r"`([^`]+)`", sql))
    approved = {
        f"ga4_bq_id.ga4_mar.{model}"
        for model in MODEL_DEFINITIONS
    }
    if not references or not references.issubset(approved):
        raise CatalogBuildError(f"{metric_id} SQL contains an unapproved table")


def _semantic_signature(row: dict[str, Any]) -> str:
    semantic = {
        "category": row["category"],
        "model": row["model"],
        "type_params": row["type_params"],
        "filter": row["filter"],
        "dimensions": row["dimensions"],
    }
    return json.dumps(semantic, ensure_ascii=False, sort_keys=True)


def _load_rows(path: Path, expected_report_type: str) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != EXPECTED_HEADERS:
            raise CatalogBuildError(
                f"Unexpected headers in {path.name}: {reader.fieldnames}"
            )
        rows = list(reader)

    for index, row in enumerate(rows, start=2):
        if row["報表類型"].strip() != expected_report_type:
            raise CatalogBuildError(
                f"{path.name}:{index} has unexpected report type {row['報表類型']!r}"
            )
        for required in ("metric_id", "main_metric", "類別", "資料表", "type_params", "sql_code"):
            if not row[required].strip():
                raise CatalogBuildError(f"{path.name}:{index} missing {required}")
    return rows


def _normalized_row(row: dict[str, str]) -> dict[str, Any]:
    model = row["資料表"].strip()
    if model not in MODEL_DEFINITIONS:
        raise CatalogBuildError(f"Unsupported model: {model}")
    category = row["類別"].strip()
    if category not in {"simple", "derived"}:
        raise CatalogBuildError(f"Unsupported category: {category}")

    type_params = _parse_type_params(row["type_params"])
    if category == "simple" and type_params.get("agg") not in set(ALLOWED_AGGREGATIONS.values()):
        raise CatalogBuildError(
            f"Unsupported aggregation for {row['metric_id']}: {type_params.get('agg')}"
        )
    if category == "derived" and not type_params.get("input_metrics"):
        raise CatalogBuildError(
            f"Derived metric {row['metric_id']} has no input_metrics"
        )
    _validate_sql_template(row["metric_id"].strip(), row["sql_code"])

    return {
        "metric_id": row["metric_id"].strip(),
        "label": row["指標名稱"].strip() or row["metric_id"].strip(),
        "main_metric": row["main_metric"].strip(),
        "category": category,
        "model": model,
        "type_params": type_params,
        "filter": _normalize_filter(row["filter"]),
        "dimensions": _dimension_ids(row["group by"]),
        "group_by_sql": row["group by"].strip(),
        "order_by_sql": row["order by"].strip(),
        "sql_template": row["sql_code"].strip(),
        "generation_note": row["sql_generation_note"].strip(),
        "report_context": {
            "page": row["頁面名稱"].strip(),
            "chart": row["圖表名稱"].strip(),
            "label": row["指標名稱"].strip(),
        },
    }


def _merge_metric_rows(
    profile: str,
    metric_id: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    by_signature: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_signature.setdefault(_semantic_signature(row), []).append(row)

    variants = []
    for signature_rows in by_signature.values():
        preferred = max(
            signature_rows,
            key=lambda item: (
                bool(item["order_by_sql"]),
                bool(item["label"] != metric_id),
            ),
        )
        variants.append(
            {
                key: preferred[key]
                for key in (
                    "label",
                    "main_metric",
                    "category",
                    "model",
                    "type_params",
                    "filter",
                    "dimensions",
                    "group_by_sql",
                    "order_by_sql",
                    "sql_template",
                    "generation_note",
                )
            }
        )

    definition_resolution = None
    policy = CANONICAL_DEFINITION_POLICIES.get((profile, metric_id))
    if policy is not None:
        matching_variants = [
            variant
            for variant in variants
            if all(
                variant.get(key) == expected
                for key, expected in policy["selector"].items()
            )
        ]
        if len(matching_variants) != 1:
            raise CatalogBuildError(
                f"Canonical policy for {profile}/{metric_id} matched "
                f"{len(matching_variants)} variants"
            )
        variants = matching_variants
        definition_resolution = policy["resolution"]

    contexts = []
    seen_contexts = set()
    for row in rows:
        marker = json.dumps(row["report_context"], ensure_ascii=False, sort_keys=True)
        if marker not in seen_contexts:
            seen_contexts.add(marker)
            contexts.append(row["report_context"])

    entry: dict[str, Any] = {
        "metric_id": metric_id,
        "status": "published" if len(variants) == 1 else "conflict",
        "contexts": contexts,
    }
    if len(variants) == 1:
        entry.update(variants[0])
        if definition_resolution is not None:
            entry["definition_resolution"] = definition_resolution
    else:
        entry["variants"] = variants
        entry["conflict_reason"] = (
            "The source contains multiple semantic definitions for this metric_id."
        )
    return entry


def build_catalog(
    non_ecommerce_path: Path,
    ecommerce_path: Path,
    catalog_version: str,
) -> dict[str, Any]:
    source_paths = {
        "non_ecommerce": non_ecommerce_path,
        "ecommerce": ecommerce_path,
    }
    profile_rows = {
        "non_ecommerce": _load_rows(non_ecommerce_path, "非電商"),
        "ecommerce": _load_rows(ecommerce_path, "電商"),
    }

    profiles: dict[str, Any] = {}
    for profile, raw_rows in profile_rows.items():
        grouped: dict[str, list[dict[str, Any]]] = {}
        for raw_row in raw_rows:
            row = _normalized_row(raw_row)
            grouped.setdefault(row["metric_id"], []).append(row)
        metrics = {
            metric_id: _merge_metric_rows(profile, metric_id, rows)
            for metric_id, rows in sorted(grouped.items())
        }
        profiles[profile] = {
            "source_row_count": len(raw_rows),
            "metric_count": len(metrics),
            "published_metric_count": sum(
                metric["status"] == "published" for metric in metrics.values()
            ),
            "metrics": metrics,
        }

    return {
        "catalog_version": catalog_version,
        "generated_on": date.today().isoformat(),
        "source_files": {
            profile: {
                "name": path.name,
                "sha256": _source_digest(path),
            }
            for profile, path in source_paths.items()
        },
        "models": MODEL_DEFINITIONS,
        "dimensions": DIMENSION_DEFINITIONS,
        "profiles": profiles,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile WayKen metric definition CSVs into a semantic catalog."
    )
    parser.add_argument("--non-ecommerce", required=True, type=Path)
    parser.add_argument("--ecommerce", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--catalog-version", default="1.0.0")
    args = parser.parse_args()

    catalog = build_catalog(
        non_ecommerce_path=args.non_ecommerce,
        ecommerce_path=args.ecommerce,
        catalog_version=args.catalog_version,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        profile: {
            key: value
            for key, value in data.items()
            if key != "metrics"
        }
        for profile, data in catalog["profiles"].items()
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
