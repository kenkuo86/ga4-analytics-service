"""Shared normalization and validation rules for the managed tenant registry."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping
import unicodedata


ALIAS_SEPARATOR = "|"
MIN_PARTIAL_SEARCH_LENGTH = 2

# These values are intentionally exact matches.  A useful alias such as
# "Sunny Digital 公司" remains valid, while a bare generic company word does
# not become a broad customer selector.
GENERIC_CUSTOMER_NAME_TERMS = frozenset(
    {
        "公司",
        "企業",
        "集團",
        "有限公司",
        "股份有限公司",
        "有限責任公司",
        "company",
        "co",
        "co.",
        "corporation",
        "corp",
        "inc",
        "inc.",
        "incorporated",
        "ltd",
        "ltd.",
        "limited",
        "group",
    }
)


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def normalize_customer_name(value: str) -> str:
    """Apply the same trim, Unicode NFKC, and casefold policy everywhere."""

    if not isinstance(value, str):
        raise TypeError("Customer names and aliases must be strings")
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def parse_aliases(value: Any) -> list[str]:
    """Parse the PoC STRING alias field, ignoring empty runtime segments."""

    if value is None:
        return []
    if not isinstance(value, str):
        raise TypeError("The tenant registry aliases field must be a STRING")
    return [part.strip() for part in value.split(ALIAS_SEPARATOR) if part.strip()]


def is_broad_customer_search(value: str) -> bool:
    normalized = normalize_customer_name(value)
    return (
        len(normalized) < MIN_PARTIAL_SEARCH_LENGTH
        or normalized in GENERIC_CUSTOMER_NAME_TERMS
    )


class TenantRegistryValidationError(ValueError):
    """Raised when registry aliases cannot safely enter the queryable set."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__("Tenant registry validation failed")
        self.code = "tenant_registry_invalid"
        self.message = "tenant registry 驗證失敗，請修正名稱或 aliases 後再發布。"
        self.issues = issues

    def as_result(self) -> dict[str, Any]:
        return {
            "status": self.code,
            "message": self.message,
            "issues": self.issues,
        }


def _issue(
    *,
    code: str,
    message: str,
    tenant_ids: Iterable[str] = (),
    alias: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "message": message,
        "tenant_ids": list(tenant_ids),
    }
    if alias is not None:
        result["alias"] = alias
    return result


def _tenant_id(row: Any, index: int) -> str:
    value = _field(row, "tenant_id")
    return str(value) if value is not None else f"row-{index + 1}"


def _validated_aliases(
    row: Any,
    tenant_id: str,
    issues: list[dict[str, Any]],
) -> list[str]:
    raw_aliases = _field(row, "aliases")
    if raw_aliases is None or (
        isinstance(raw_aliases, str) and not raw_aliases.strip()
    ):
        return []
    if not isinstance(raw_aliases, str):
        issues.append(
            _issue(
                code="invalid_alias_type",
                message="aliases 必須是 STRING。",
                tenant_ids=[tenant_id],
            )
        )
        return []

    raw_parts = raw_aliases.split(ALIAS_SEPARATOR)
    if any(not part.strip() for part in raw_parts):
        issues.append(
            _issue(
                code="empty_alias",
                message="aliases 不得包含空白 alias；空白欄位請留空。",
                tenant_ids=[tenant_id],
            )
        )
    return [part.strip() for part in raw_parts if part.strip()]


def validate_registry_rows(rows: Iterable[Any]) -> dict[str, Any]:
    """Validate formal names and managed aliases before a registry rollout.

    The function returns a compact report on success and raises
    ``TenantRegistryValidationError`` on any collision or malformed alias.
    """

    issues: list[dict[str, Any]] = []
    formal_names: dict[str, list[str]] = defaultdict(list)
    alias_names: dict[str, list[str]] = defaultdict(list)
    formal_display: dict[str, str] = {}
    alias_display: dict[str, str] = {}
    tenant_count = 0
    skipped_unnamed_tenant_count = 0
    alias_count = 0

    for index, row in enumerate(rows):
        tenant_count += 1
        tenant_id = _tenant_id(row, index)
        raw_name = _field(row, "tenant_name")
        if raw_name is None or (
            isinstance(raw_name, str) and not normalize_customer_name(raw_name)
        ):
            skipped_unnamed_tenant_count += 1
            continue
        if not isinstance(raw_name, str):
            issues.append(
                _issue(
                    code="invalid_tenant_name",
                    message="tenant_name 必須是 STRING。",
                    tenant_ids=[tenant_id],
                )
            )
            continue

        normalized_name = normalize_customer_name(raw_name)
        formal_names[normalized_name].append(tenant_id)
        formal_display.setdefault(normalized_name, raw_name.strip())

        normalized_aliases: set[str] = set()
        for alias in _validated_aliases(row, tenant_id, issues):
            normalized_alias = normalize_customer_name(alias)
            if not normalized_alias:
                continue
            alias_count += 1
            if normalized_alias in GENERIC_CUSTOMER_NAME_TERMS:
                issues.append(
                    _issue(
                        code="generic_alias",
                        message="alias 不得只有通用公司詞。",
                        tenant_ids=[tenant_id],
                        alias=alias,
                    )
                )
            if normalized_alias in normalized_aliases:
                issues.append(
                    _issue(
                        code="duplicate_alias_within_tenant",
                        message="同一 tenant 不得登記正規化後重複的 alias。",
                        tenant_ids=[tenant_id],
                        alias=alias,
                    )
                )
            normalized_aliases.add(normalized_alias)
            alias_names[normalized_alias].append(tenant_id)
            alias_display.setdefault(normalized_alias, alias)

            if normalized_alias == normalized_name:
                issues.append(
                    _issue(
                        code="alias_matches_formal_name",
                        message="alias 不得與同一 tenant 的正式名稱重複。",
                        tenant_ids=[tenant_id],
                        alias=alias,
                    )
                )

    for normalized_name, tenant_ids in formal_names.items():
        if len(set(tenant_ids)) > 1:
            issues.append(
                _issue(
                    code="duplicate_formal_name",
                    message=(
                        f"正式名稱「{formal_display[normalized_name]}」正規化後對應多個 tenants。"
                    ),
                    tenant_ids=sorted(set(tenant_ids)),
                )
            )

    for normalized_alias, tenant_ids in alias_names.items():
        unique_tenant_ids = sorted(set(tenant_ids))
        if len(unique_tenant_ids) > 1:
            issues.append(
                _issue(
                    code="duplicate_alias_across_tenants",
                    message=(
                        f"alias「{alias_display[normalized_alias]}」正規化後不得指向多個 tenants。"
                    ),
                    tenant_ids=unique_tenant_ids,
                    alias=alias_display[normalized_alias],
                )
            )
        formal_tenant_ids = formal_names.get(normalized_alias, [])
        conflicting_tenant_ids = sorted(
            set(unique_tenant_ids).union(formal_tenant_ids)
        )
        if formal_tenant_ids and set(formal_tenant_ids) != set(unique_tenant_ids):
            issues.append(
                _issue(
                    code="alias_conflicts_with_formal_name",
                    message=(
                        f"alias「{alias_display[normalized_alias]}」不得與其他 tenant 的正式名稱衝突。"
                    ),
                    tenant_ids=conflicting_tenant_ids,
                    alias=alias_display[normalized_alias],
                )
            )

    report = {
        "status": "passed" if not issues else "failed",
        "tenant_count": tenant_count,
        "validated_tenant_count": tenant_count - skipped_unnamed_tenant_count,
        "skipped_unnamed_tenant_count": skipped_unnamed_tenant_count,
        "alias_count": alias_count,
        "issue_count": len(issues),
        "alias_separator": ALIAS_SEPARATOR,
    }
    if issues:
        raise TenantRegistryValidationError(issues)
    return report
