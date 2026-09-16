"""Versioned period vocabulary and deterministic intent-level date parsing.

The connector uses this module as the single source of truth for relative
period phrases.  It deliberately models the user's requested calendar days
separately from the periods that an individual data tool may scan.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import re
import unicodedata
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from query_policy import (
    DEFAULT_EARLIEST_DATE,
    DEFAULT_MAX_DATE_RANGE_DAYS,
    DEFAULT_TIME_ZONE,
)


PERIOD_PHRASE_CONTRACT_VERSION = "1.0.6"
PERIOD_OUTCOMES = ("resolved", "needs_clarification", "invalid_period")

_CHINESE_DIGIT_VALUES = {
    "零": 0,
    "〇": 0,
    "○": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_NUMBER_CHARS = "零〇○一二兩两三四五六七八九十百千万萬億亿"
_ENGLISH_NUMBER_VALUES = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def _alternatives(values: tuple[str, ...] | list[str]) -> str:
    return (
        "(?:"
        + "|".join(re.escape(value) for value in sorted(values, key=len, reverse=True))
        + ")"
    )


_CHINESE_QUANTITY = rf"(?:\d+|[{_CHINESE_NUMBER_CHARS}]+)"
_ENGLISH_QUANTITY = _alternatives(tuple(_ENGLISH_NUMBER_VALUES))
_QUANTITY = rf"(?:{_CHINESE_QUANTITY}|{_ENGLISH_QUANTITY})"

# This is intentionally data, rather than prose repeated in each tool
# description.  Parser patterns below are built from these same aliases.
PHRASE_FAMILIES = (
    {
        "family_id": "single_day",
        "window_kind": "single_day",
        "phrases": ("今天", "昨天", "today", "yesterday"),
        "fixed_aliases": (
            ("今天", "today"),
            ("昨天", "yesterday"),
            ("today", "today"),
            ("yesterday", "yesterday"),
        ),
        "semantics": "對應的單一 calendar day。",
    },
    {
        "family_id": "rolling_days",
        "window_kind": "rolling_days",
        "phrases": (
            "過去／最近／近 N 天／日",
            "past／recent／last N days",
        ),
        "aliases": {
            "zh_prefixes": ("過去", "最近", "近"),
            "zh_units": ("天", "日"),
            "en_prefixes": ("past", "recent", "last"),
            "en_units": ("day", "days"),
        },
        "semantics": "包含 today；start=today-(N-1 days)、end=today。",
    },
    {
        "family_id": "previous_days",
        "window_kind": "previous_days",
        "phrases": (
            "前 N 天／日",
            "previous N days、last day",
        ),
        "aliases": {
            "zh_prefixes": ("前",),
            "zh_units": ("天", "日"),
            "en_prefixes": ("previous",),
            "en_units": ("day", "days"),
        },
        "fixed_aliases": (("last day", "last day"),),
        "semantics": "不包含 today；start=today-N days、end=yesterday。",
    },
    {
        "family_id": "rolling_weeks",
        "window_kind": "rolling_weeks",
        "phrases": (
            "過去／最近／近 N 週／周／星期",
            "past／recent／last N weeks",
        ),
        "aliases": {
            "zh_prefixes": ("過去", "最近", "近"),
            "zh_units": ("週", "周", "星期"),
            "en_prefixes": ("past", "recent", "last"),
            "en_units": ("week", "weeks"),
        },
        "semantics": "包含 today 的連續 7*N 天，不依 calendar week 切齊。",
    },
    {
        "family_id": "completed_weeks",
        "window_kind": "completed_weeks",
        "phrases": (
            "前 N 週／周／星期、上週",
            "previous N weeks、last week",
        ),
        "aliases": {
            "zh_prefixes": ("前",),
            "zh_units": ("週", "周", "星期"),
            "en_prefixes": ("previous",),
            "en_units": ("week", "weeks"),
        },
        "fixed_aliases": (
            ("上週", "last week"),
            ("上周", "last week"),
            ("上一週", "last week"),
            ("上一周", "last week"),
            ("last week", "last week"),
        ),
        "semantics": "ISO week（週一至週日），不包含本週。",
    },
    {
        "family_id": "week_to_date",
        "window_kind": "week_to_date",
        "phrases": ("本週／本周、這週／這周", "this week"),
        "aliases": {"zh_prefixes": ("本", "這")},
        "fixed_aliases": (
            ("本週", "this week"),
            ("本周", "this week"),
            ("這週", "this week"),
            ("這周", "this week"),
            ("this week", "this week"),
        ),
        "semantics": "本週一至 today。",
    },
    {
        "family_id": "rolling_months",
        "window_kind": "rolling_months",
        "phrases": (
            "過去／最近／近 N 個月、過去／最近／近半年",
            "past／recent／last N months",
        ),
        "aliases": {
            "zh_prefixes": ("過去", "最近", "近"),
            "zh_units": ("個月", "个月", "月"),
            "en_prefixes": ("past", "recent", "last"),
            "en_units": ("month", "months"),
            "special_quantities": {
                "zh": (("半年", 6),),
                "en": (("half year", 6), ("half a year", 6)),
            },
        },
        "semantics": "往前移 N 個 calendar months，目標日不存在時 clamp 至月底，再加一天為 start；end=today。",
    },
    {
        "family_id": "completed_months",
        "window_kind": "completed_months",
        "phrases": (
            "前 N 個月、上個月／上一個月",
            "previous N months、last month",
        ),
        "aliases": {
            "zh_prefixes": ("前",),
            "zh_units": ("個月", "个月", "月"),
            "en_prefixes": ("previous",),
            "en_units": ("month", "months"),
        },
        "candidate_prefixes": ("上一個",),
        "fixed_aliases": (
            ("上個月", "last month"),
            ("上个月", "last month"),
            ("上一個月", "last month"),
            ("last month", "last month"),
        ),
        "semantics": "完整 calendar months，不包含本月。",
    },
    {
        "family_id": "month_to_date",
        "window_kind": "month_to_date",
        "phrases": ("本月、這個月／这个月", "this month"),
        "aliases": {"zh_prefixes": ("本", "這")},
        "fixed_aliases": (
            ("本月", "this month"),
            ("這個月", "this month"),
            ("这个月", "this month"),
            ("this month", "this month"),
        ),
        "semantics": "本月第一天至 today。",
    },
    {
        "family_id": "rolling_years",
        "window_kind": "rolling_years",
        "phrases": (
            "過去／最近／近 N 年",
            "past／recent／last N years",
        ),
        "aliases": {
            "zh_prefixes": ("過去", "最近", "近"),
            "zh_units": ("年",),
            "en_prefixes": ("past", "recent", "last"),
            "en_units": ("year", "years"),
        },
        "semantics": "往前移 N 年，02-29 在非閏年 clamp 至 02-28，再加一天為 start；end=today。",
    },
    {
        "family_id": "completed_years",
        "window_kind": "completed_years",
        "phrases": ("前 N 年、去年", "previous N years、last year"),
        "aliases": {
            "zh_prefixes": ("前",),
            "zh_units": ("年",),
            "en_prefixes": ("previous",),
            "en_units": ("year", "years"),
        },
        "fixed_aliases": (
            ("去年", "last year"),
            ("last year", "last year"),
        ),
        "semantics": "完整 calendar years，不包含今年。",
    },
    {
        "family_id": "year_to_date",
        "window_kind": "year_to_date",
        "phrases": ("今年", "this year"),
        "fixed_aliases": (("今年", "this year"), ("this year", "this year")),
        "semantics": "當年 01-01 至 today。",
    },
    {
        "family_id": "explicit_date",
        "window_kind": "explicit_date",
        "phrases": (
            "單一 YYYY-MM-DD",
            "YYYY-MM-DD 到／至／to／through／~／－／- YYYY-MM-DD",
        ),
        "semantics": "單一日期或包含起訖兩端的明確日期範圍。",
    },
)


def _family(family_id: str) -> dict[str, Any]:
    return next(
        family for family in PHRASE_FAMILIES if family["family_id"] == family_id
    )


def _family_aliases(family_id: str, alias_name: str) -> tuple[str, ...]:
    aliases = _family(family_id).get("aliases", {}).get(alias_name, ())
    return tuple(aliases)


_ROLLING_ZH_PREFIXES = _family_aliases("rolling_days", "zh_prefixes")
_PREVIOUS_ZH_PREFIXES = _family_aliases("previous_days", "zh_prefixes")
_TO_DATE_ZH_PREFIXES = _family_aliases("week_to_date", "zh_prefixes")
_THIS_EN_PREFIX = next(
    phrase.split()[0]
    for phrase, canonical in _family("week_to_date")["fixed_aliases"]
    if canonical == "this week" and phrase.isascii()
)
_ALL_ROLLING_EN_PREFIXES = _family_aliases("rolling_days", "en_prefixes")
_LAST_EN_PREFIX = next(
    prefix for prefix in _ALL_ROLLING_EN_PREFIXES if prefix == "last"
)
_ROLLING_EN_PREFIXES = tuple(
    prefix for prefix in _ALL_ROLLING_EN_PREFIXES if prefix != _LAST_EN_PREFIX
)

_DAY_ZH_UNITS = _family_aliases("rolling_days", "zh_units")
_WEEK_ZH_UNITS = _family_aliases("rolling_weeks", "zh_units")
_MONTH_ZH_UNITS = _family_aliases("rolling_months", "zh_units")
_YEAR_ZH_UNITS = _family_aliases("rolling_years", "zh_units")

_DAY_EN_UNITS = _family_aliases("rolling_days", "en_units")
_WEEK_EN_UNITS = _family_aliases("rolling_weeks", "en_units")
_MONTH_EN_UNITS = _family_aliases("rolling_months", "en_units")
_YEAR_EN_UNITS = _family_aliases("rolling_years", "en_units")
_ALL_EN_PERIOD_UNITS = tuple(
    dict.fromkeys(
        unit
        for family_id in (
            "rolling_days",
            "rolling_weeks",
            "rolling_months",
            "rolling_years",
        )
        for unit in _family_aliases(family_id, "en_units")
    )
)
_ALL_ZH_PERIOD_UNITS = tuple(
    dict.fromkeys(
        unit
        for family_id in (
            "rolling_days",
            "rolling_weeks",
            "rolling_months",
            "rolling_years",
        )
        for unit in _family_aliases(family_id, "zh_units")
    )
)

_PREVIOUS_EN_PREFIXES = _family_aliases("previous_days", "en_prefixes")
_COMPLETED_WEEK_EN_PREFIXES = _family_aliases("completed_weeks", "en_prefixes")
_COMPLETED_MONTH_EN_PREFIXES = _family_aliases("completed_months", "en_prefixes")
_COMPLETED_YEAR_EN_PREFIXES = _family_aliases("completed_years", "en_prefixes")
_ROLLING_MONTH_EN_PREFIXES = _family_aliases("rolling_months", "en_prefixes")
_ALL_RELATIVE_EN_PREFIXES = tuple(
    dict.fromkeys(
        prefix
        for family_id in (
            "rolling_days",
            "previous_days",
            "completed_weeks",
            "completed_months",
            "completed_years",
        )
        for prefix in _family_aliases(family_id, "en_prefixes")
    )
)

COMPARISON_MODIFIER_PHRASES = (
    "與前期比較",
    "與上一期比較",
    "跟前期比較",
    "和前期比較",
    "與前期對比",
    "前期比較",
    "前期對比",
    "上一期比較",
    "previous period",
    "the previous period",
    "compare with the previous period",
    "compare to the previous period",
    "compared with the previous period",
    "compared to the previous period",
)

GROUPING_QUALIFIER_PHRASES = (
    "按日",
    "按天",
    "按週",
    "按周",
    "按星期",
    "按月",
    "按年",
    "每日",
    "每天",
    "每週",
    "每周",
    "每月",
    "每年",
    "daily",
    "weekly",
    "monthly",
    "yearly",
)

EXPLICIT_DATE_RANGE_SEPARATORS = (
    "到",
    "至",
    "to",
    "through",
    "~",
    "～",
    "－",
    "–",
    "—",
    "-",
)

INDEPENDENT_DATE_PERIOD_JOINERS = (
    "and",
    "與",
    "和",
    "及",
    "以及",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _fixed_aliases(family: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for item in family.get("fixed_aliases", ()):
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            raise RuntimeError(
                f"Invalid fixed period alias in {family.get('family_id')!r}"
            )
        phrase, canonical = item
        result.append((phrase, canonical))
    return tuple(result)


def _phrase_templates(family: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        phrase for phrase in family.get("phrases", ()) if isinstance(phrase, str)
    )


def _window_kind(family: Mapping[str, Any]) -> str:
    value = family.get("window_kind")
    if not isinstance(value, str):
        raise RuntimeError(f"Invalid period window kind in {family.get('family_id')!r}")
    return value


def _contract_aliases(family: Mapping[str, Any]) -> Mapping[str, Any]:
    value = family.get("aliases", {})
    return value if isinstance(value, Mapping) else {}


def _candidate_prefixes(family: Mapping[str, Any]) -> tuple[str, ...]:
    value = family.get("candidate_prefixes", ())
    return tuple(item for item in value if isinstance(item, str))


def _special_quantity_aliases(
    family: Mapping[str, Any], language: str
) -> tuple[tuple[str, int], ...]:
    value = _contract_aliases(family).get("special_quantities", {})
    if not isinstance(value, Mapping):
        return ()
    aliases = value.get(language, ())
    return tuple(
        (alias, quantity)
        for item in aliases
        if isinstance(item, (tuple, list))
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], int)
        for alias, quantity in [(item[0], item[1])]
    )


def _all_candidate_zh_prefixes() -> tuple[str, ...]:
    prefixes: list[str] = []
    for family in PHRASE_FAMILIES:
        aliases = _contract_aliases(family)
        prefixes.extend(
            item for item in aliases.get("zh_prefixes", ()) if isinstance(item, str)
        )
        prefixes.extend(_candidate_prefixes(family))
    return tuple(dict.fromkeys(prefixes))


_ROLLING_MONTH_SPECIAL_QUANTITIES = {
    "zh": _special_quantity_aliases(_family("rolling_months"), "zh"),
    "en": _special_quantity_aliases(_family("rolling_months"), "en"),
}
_HALF_YEAR_ZH_ALIASES = tuple(
    alias for alias, _quantity in _ROLLING_MONTH_SPECIAL_QUANTITIES["zh"]
)
_HALF_YEAR_EN_ALIASES = tuple(
    alias for alias, _quantity in _ROLLING_MONTH_SPECIAL_QUANTITIES["en"]
)
_CANDIDATE_ZH_PREFIXES = _all_candidate_zh_prefixes()


PERIOD_PHRASE_CONTRACT = {
    "version": PERIOD_PHRASE_CONTRACT_VERSION,
    "outcomes": list(PERIOD_OUTCOMES),
    "quantity": {
        "arabic": "正整數阿拉伯數字",
        "chinese": sorted(_CHINESE_DIGIT_VALUES),
        "english": list(_ENGLISH_NUMBER_VALUES),
        "zero_or_negative": "invalid_period",
        "unparseable": "needs_clarification",
    },
    "phrase_families": [
        _json_safe(
            {
                "family_id": family["family_id"],
                "window_kind": _window_kind(family),
                "phrases": list(_phrase_templates(family)),
                "aliases": family.get("aliases", {}),
                "candidate_prefixes": _candidate_prefixes(family),
                "fixed_aliases": _fixed_aliases(family),
                "semantics": family["semantics"],
            }
        )
        for family in PHRASE_FAMILIES
    ],
    "comparison_modifiers": list(COMPARISON_MODIFIER_PHRASES),
    "grouping_qualifiers": list(GROUPING_QUALIFIER_PHRASES),
    "fallbacks": [
        {
            "pattern": "zero_or_negative_quantity",
            "outcome": "invalid_period",
        },
        {
            "pattern": "fractional_period_quantity",
            "outcome": "invalid_period",
        },
        {
            "pattern": "unparseable_quantity_or_unnatural_combination",
            "outcome": "needs_clarification",
        },
        {
            "pattern": "slash_date_or_invalid_iso_date",
            "outcome": "invalid_period",
        },
        {
            "pattern": "unsupported_date_range_connector",
            "outcome": "needs_clarification",
        },
        {
            "pattern": "incomplete_date_range",
            "outcome": "needs_clarification",
        },
        {
            "pattern": "unconsumed_period_residue",
            "outcome": "needs_clarification",
        },
        {
            "pattern": "punctuation_delimited_relative_period",
            "outcome": "needs_clarification",
        },
        {
            "pattern": "punctuation_delimited_period_quantity",
            "outcome": "needs_clarification",
        },
        {
            "pattern": "comparison_modifier_without_supported_report_contract",
            "outcome": "needs_clarification",
        },
    ],
    "explicit_date": {
        "format": "YYYY-MM-DD",
        "range_separators": list(EXPLICIT_DATE_RANGE_SEPARATORS),
        "independent_period_joiners": list(INDEPENDENT_DATE_PERIOD_JOINERS),
        "slash_format": "invalid_period",
    },
}

# Lower-case alias makes the contract name used in the roadmap directly
# importable while retaining the conventional constant spelling.
period_phrase_contract = PERIOD_PHRASE_CONTRACT


@dataclass(frozen=True)
class PeriodInterval:
    phrase: str
    window_kind: str
    start_date: date
    end_date: date
    source: str = "explicit"

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "window_kind": self.window_kind,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "days": self.days,
            "source": self.source,
        }


@dataclass(frozen=True)
class PeriodPhraseMatch:
    phrase: str
    outcome: str
    span: tuple[int, int]
    window_kind: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    reason_code: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phrase": self.phrase,
            "outcome": self.outcome,
        }
        if self.window_kind is not None:
            result["window_kind"] = self.window_kind
        if self.start_date is not None:
            result["start_date"] = self.start_date.isoformat()
        if self.end_date is not None:
            result["end_date"] = self.end_date.isoformat()
        if self.reason_code is not None:
            result["reason_code"] = self.reason_code
        if self.message is not None:
            result["message"] = self.message
        return result


@dataclass(frozen=True)
class PeriodIntent:
    """Normalized user period intent, independent from a query scan period."""

    outcome: str
    explicit_periods: tuple[PeriodInterval, ...] = ()
    implicit_periods: tuple[PeriodInterval, ...] = ()
    requested_days: int = 0
    max_days: int = DEFAULT_MAX_DATE_RANGE_DAYS
    phrase_matches: tuple[PeriodPhraseMatch, ...] = ()
    comparison_modifier: bool = False
    reason_code: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "contract_version": PERIOD_PHRASE_CONTRACT_VERSION,
            "outcome": self.outcome,
            "explicit_periods": [period.as_dict() for period in self.explicit_periods],
            "requested_days": self.requested_days,
            "max_days": self.max_days,
            "implicit_periods": [period.as_dict() for period in self.implicit_periods],
            "comparison_modifier": self.comparison_modifier,
            "phrase_matches": [match.as_dict() for match in self.phrase_matches],
        }
        if self.reason_code is not None:
            result["reason_code"] = self.reason_code
        if self.message is not None:
            result["message"] = self.message
        return result


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(normalized.split())


def _policy_value(policy: Any, name: str, default: Any) -> Any:
    if policy is not None and hasattr(policy, name):
        return getattr(policy, name)
    try:
        from query_policy import query_policy as active_policy

        return getattr(active_policy, name)
    except Exception:
        return default


def _today(policy: Any, today: date | None) -> date:
    if today is not None:
        return today
    time_zone = _policy_value(policy, "time_zone", DEFAULT_TIME_ZONE)
    return datetime.now(ZoneInfo(time_zone)).date()


def _parse_chinese_number(value: str) -> int | None:
    normalized = value.replace("两", "兩").replace("〇", "零").replace("○", "零")
    if not normalized or any(char not in _CHINESE_NUMBER_CHARS for char in normalized):
        return None

    if all(char in _CHINESE_DIGIT_VALUES for char in normalized):
        if len(normalized) == 1:
            return _CHINESE_DIGIT_VALUES[normalized]
        # A sequence such as 一三 is not a natural cardinal number in this
        # vocabulary.  It is safer to ask than to infer thirteen.
        return None

    unit_values = {
        "十": 10,
        "百": 100,
        "千": 1000,
        "萬": 10_000,
        "万": 10_000,
        "億": 100_000_000,
        "亿": 100_000_000,
    }
    total = 0
    section = 0
    number = 0
    last_unit = float("inf")
    for char in normalized:
        if char in _CHINESE_DIGIT_VALUES:
            number = _CHINESE_DIGIT_VALUES[char]
            continue
        unit = unit_values.get(char)
        if unit is None or unit >= last_unit:
            return None
        if number == 0:
            number = 1
        if unit < 10_000:
            section += number * unit
        else:
            section = (section + number) * unit
            total += section
            section = 0
        number = 0
        last_unit = unit
    return total + section + number


def parse_quantity(value: str | None) -> int | None:
    """Parse an explicitly supported Arabic, Chinese, or English quantity."""

    if value is None:
        return None
    normalized = _normalize(value)
    if normalized.isdigit():
        try:
            return int(normalized)
        except ValueError:
            # Python can reject an excessively long digit string before the
            # period resolver gets a chance to return a structured fallback.
            return None
    if normalized in _ENGLISH_NUMBER_VALUES:
        return _ENGLISH_NUMBER_VALUES[normalized]
    return _parse_chinese_number(normalized)


def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + (value.month - 1) + months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _shift_years(value: date, years: int) -> date:
    target_year = value.year + years
    day = min(value.day, monthrange(target_year, value.month)[1])
    return date(target_year, value.month, day)


def _relative_dates(
    window_kind: str,
    quantity: int,
    anchor: date,
) -> tuple[date, date]:
    if window_kind == "rolling_days":
        return anchor - timedelta(days=quantity - 1), anchor
    if window_kind == "previous_days":
        return anchor - timedelta(days=quantity), anchor - timedelta(days=1)
    if window_kind == "rolling_weeks":
        day_count = quantity * 7
        return anchor - timedelta(days=day_count - 1), anchor
    if window_kind == "completed_weeks":
        current_monday = anchor - timedelta(days=anchor.weekday())
        end = current_monday - timedelta(days=1)
        return end - timedelta(days=quantity * 7 - 1), end
    if window_kind == "rolling_months":
        return _shift_months(anchor, -quantity) + timedelta(days=1), anchor
    if window_kind == "completed_months":
        current_month = anchor.replace(day=1)
        end = current_month - timedelta(days=1)
        start = _shift_months(current_month, -quantity)
        return start, end
    if window_kind == "rolling_years":
        return _shift_years(anchor, -quantity) + timedelta(days=1), anchor
    if window_kind == "completed_years":
        return date(anchor.year - quantity, 1, 1), date(anchor.year - 1, 12, 31)
    raise ValueError(f"Unsupported relative window kind: {window_kind}")


_FIXED_PHRASES: tuple[tuple[str, str, str], ...] = tuple(
    (phrase, _window_kind(family), canonical)
    for family in PHRASE_FAMILIES
    for phrase, canonical in _fixed_aliases(family)
)


_FIXED_PATTERN = re.compile(
    rf"(?<![a-z0-9]){_alternatives(tuple(item[0] for item in _FIXED_PHRASES))}(?![a-z0-9])"
)
_FIXED_BY_PHRASE = {
    phrase: (kind, canonical) for phrase, kind, canonical in _FIXED_PHRASES
}


@dataclass(frozen=True)
class _RelativeRule:
    window_kind: str
    pattern: re.Pattern[str]
    fixed_quantity: int | None = None


_RELATIVE_RULES = (
    _RelativeRule(
        "rolling_days",
        re.compile(
            rf"(?<![a-z0-9])(?:{_alternatives(_ROLLING_ZH_PREFIXES)})\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_DAY_ZH_UNITS)}"
            rf"|(?<![a-z0-9])(?:{_alternatives(_ROLLING_EN_PREFIXES)})\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_DAY_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_days",
        re.compile(
            rf"(?<![a-z0-9]){re.escape(_LAST_EN_PREFIX)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_DAY_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "previous_days",
        re.compile(
            rf"(?<![a-z0-9]){_alternatives(_PREVIOUS_ZH_PREFIXES)}\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_DAY_ZH_UNITS)}"
            rf"|(?<![a-z0-9]){_alternatives(_PREVIOUS_EN_PREFIXES)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_DAY_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_weeks",
        re.compile(
            rf"(?<![a-z0-9])(?:{_alternatives(_ROLLING_ZH_PREFIXES)})\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_WEEK_ZH_UNITS)}"
            rf"|(?<![a-z0-9])(?:{_alternatives(_ROLLING_EN_PREFIXES)})\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_WEEK_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_weeks",
        re.compile(
            rf"(?<![a-z0-9]){re.escape(_LAST_EN_PREFIX)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_WEEK_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "completed_weeks",
        re.compile(
            rf"(?<![a-z0-9]){_alternatives(_PREVIOUS_ZH_PREFIXES)}\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_WEEK_ZH_UNITS)}"
            rf"|(?<![a-z0-9]){_alternatives(_COMPLETED_WEEK_EN_PREFIXES)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_WEEK_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_months",
        re.compile(
            rf"(?<![a-z0-9])(?:{_alternatives(_ROLLING_ZH_PREFIXES)})\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_MONTH_ZH_UNITS)}"
            rf"|(?<![a-z0-9])(?:{_alternatives(_ROLLING_EN_PREFIXES)})\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_MONTH_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_months",
        re.compile(
            rf"(?<![a-z0-9]){re.escape(_LAST_EN_PREFIX)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_MONTH_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "completed_months",
        re.compile(
            rf"(?<![a-z0-9]){_alternatives(_PREVIOUS_ZH_PREFIXES)}\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_MONTH_ZH_UNITS)}"
            rf"|(?<![a-z0-9]){_alternatives(_COMPLETED_MONTH_EN_PREFIXES)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_MONTH_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_years",
        re.compile(
            rf"(?<![a-z0-9])(?:{_alternatives(_ROLLING_ZH_PREFIXES)})\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_YEAR_ZH_UNITS)}"
            rf"|(?<![a-z0-9])(?:{_alternatives(_ROLLING_EN_PREFIXES)})\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_YEAR_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_years",
        re.compile(
            rf"(?<![a-z0-9]){re.escape(_LAST_EN_PREFIX)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_YEAR_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "completed_years",
        re.compile(
            rf"(?<![a-z0-9]){_alternatives(_PREVIOUS_ZH_PREFIXES)}\s*(?P<zh_quantity>{_QUANTITY})\s*{_alternatives(_YEAR_ZH_UNITS)}"
            rf"|(?<![a-z0-9]){_alternatives(_COMPLETED_YEAR_EN_PREFIXES)}\s+(?P<en_quantity>{_QUANTITY})\s+{_alternatives(_YEAR_EN_UNITS)}(?![a-z0-9])"
        ),
    ),
    _RelativeRule(
        "rolling_months",
        re.compile(
            rf"(?<![a-z0-9])(?:{_alternatives(_ROLLING_ZH_PREFIXES)})\s*{_alternatives(_HALF_YEAR_ZH_ALIASES)}"
            rf"|(?<![a-z0-9])(?:{_alternatives(_ROLLING_MONTH_EN_PREFIXES)})\s+{_alternatives(_HALF_YEAR_EN_ALIASES)}(?![a-z0-9])"
        ),
        fixed_quantity=6,
    ),
)


_ISO_DATE_LIKE = r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
_DATE_TOKEN_BOUNDARY = r"[A-Za-z0-9]"
_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_RANGE_PATTERN = re.compile(
    rf"(?<!{_DATE_TOKEN_BOUNDARY})(?P<start>{_ISO_DATE_LIKE})(?!{_DATE_TOKEN_BOUNDARY})"
    rf"\s*(?P<separator>{_alternatives(EXPLICIT_DATE_RANGE_SEPARATORS)})\s*"
    rf"(?<!{_DATE_TOKEN_BOUNDARY})(?P<end>{_ISO_DATE_LIKE})(?!{_DATE_TOKEN_BOUNDARY})"
)
_DATE_SINGLE_PATTERN = re.compile(
    rf"(?<!{_DATE_TOKEN_BOUNDARY}){_ISO_DATE_LIKE}(?!{_DATE_TOKEN_BOUNDARY})"
)
_DATE_PERIOD_JOINER_PATTERN = re.compile(
    rf"\s*(?:"
    rf"[,，、;；:：]+\s*(?:(?:{_alternatives(INDEPENDENT_DATE_PERIOD_JOINERS)})(?![a-z0-9]))?"
    rf"|(?:{_alternatives(INDEPENDENT_DATE_PERIOD_JOINERS)})(?![a-z0-9])"
    rf")(?:\s+.*)?"
)
# Keep a second candidate pattern so a date with attached characters, invalid
# component widths, or missing/non-numeric components (for example
# 2026-09-01abc, 2026/009/01, or 2026--09-01) is rejected instead of silently
# shortened.  A candidate must start at a numeric year-shaped token.  Its first
# two components must then remain date-shaped (numeric or missing), except for a
# short alphabetic placeholder followed by a numeric day.  This deliberately
# excludes ordinary identifiers such as ``summer2026-sale-us``,
# ``2026-q1-sales``, and ``2026-09-sale``.
_DATE_LIKE_CANDIDATE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])\d{4,}\s*[-/]\s*(?:"
    r"(?:\d+[A-Za-z_]*|)\s*[-/]\s*(?:\d+[A-Za-z_]*|)"
    r"|[A-Za-z_]{1,2}\s*[-/]\s*\d+[A-Za-z_]*"
    r")(?![A-Za-z0-9_])"
)
_DATE_CONTEXT_CANDIDATE_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![a-z0-9])from(?![a-z0-9])"
    rf"|(?<![a-z0-9])(?:date|dates)(?![a-z0-9])(?:\s+(?:is|from))?"
    rf"|日期(?:\s*(?:是|為|为|[:：]))?"
    rf"|{_alternatives(('自', '從', '从'))}"
    rf")\s*(?P<candidate>\d{{4,}}\s*[-/]\s*[A-Za-z0-9_]*\s*[-/]\s*[A-Za-z0-9_]*)"
)
_DATE_RANGE_TAIL_CANDIDATE_PATTERN = re.compile(
    rf"\s*(?:{_alternatives(EXPLICIT_DATE_RANGE_SEPARATORS)})\s*"
    rf"(?P<candidate>\d{{4,}}\s*[-/]\s*[A-Za-z0-9_]*\s*[-/]\s*[A-Za-z0-9_]*)"
)
# A complete ISO date or an already malformed three-component token is
# handled by the parser above.  These two patterns cover the remaining
# date-shaped signals that must not disappear when recognition stops early,
# such as ``2026-09`` or ``date 2026``.
_DATE_PARTIAL_CANDIDATE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])\d{4,}\s*[-/]\s*\d{1,2}" r"(?!\s*[-/]|[A-Za-z0-9_])"
)
_DATE_CONTEXT_PARTIAL_CANDIDATE_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![a-z0-9])from(?![a-z0-9])"
    rf"|(?<![a-z0-9])(?:date|dates)(?![a-z0-9])(?:\s+(?:is|from))?"
    rf"|日期(?:\s*(?:是|為|为|[:：]))?"
    rf"|{_alternatives(('自', '從', '从'))}"
    rf")\s*(?P<candidate>\d{{4,}}(?:\s*[-/]\s*\d{{1,2}})?)"
    r"(?!\s*[-/]|[A-Za-z0-9_])"
)
_FRACTIONAL_QUANTITY = r"[-−－]?\s*\d+[.．]\d+"
_FRACTIONAL_PERIOD_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![a-z0-9])(?:{_alternatives(_ALL_RELATIVE_EN_PREFIXES + (_THIS_EN_PREFIX,))})\s+"
    rf"{_FRACTIONAL_QUANTITY}\s+{_alternatives(_ALL_EN_PERIOD_UNITS + _HALF_YEAR_EN_ALIASES)}(?![a-z0-9])"
    rf"|(?<![a-z0-9])(?:{_alternatives(_CANDIDATE_ZH_PREFIXES)})\s*"
    rf"{_FRACTIONAL_QUANTITY}\s*{_alternatives(_ALL_ZH_PERIOD_UNITS)}"
    rf")"
)

_COMPARISON_PATTERN = re.compile(
    rf"(?<![a-z0-9]){_alternatives(COMPARISON_MODIFIER_PHRASES)}(?![a-z0-9])"
)

# This intentionally catches malformed quantities and old slash-date forms so
# that they receive an explicit fallback instead of being silently removed as
# a harmless qualifier.
_PERIOD_CANDIDATE_PATTERN = re.compile(
    rf"(?:"
    rf"(?:{_alternatives(_CANDIDATE_ZH_PREFIXES)})"
    rf"\s*(?:[-−－]?\s*[\w零〇○一二兩两三四五六七八九十百千万萬億亿]+)?\s*"
    rf"(?:{_alternatives(_ALL_ZH_PERIOD_UNITS + _HALF_YEAR_ZH_ALIASES)})"
    rf"|(?:{_alternatives(_ALL_RELATIVE_EN_PREFIXES + (_THIS_EN_PREFIX,))})\s+"
    rf"[^,.;，。；]*?(?:{_alternatives(_ALL_EN_PERIOD_UNITS + _HALF_YEAR_EN_ALIASES)})"
    rf"|{_alternatives(_HALF_YEAR_ZH_ALIASES)}|{_alternatives(_HALF_YEAR_EN_ALIASES)}"
    rf")"
)
_PERIOD_UNIT_ALIASES = tuple(
    dict.fromkeys(
        (
            *_ALL_ZH_PERIOD_UNITS,
            *_ALL_EN_PERIOD_UNITS,
            *_HALF_YEAR_ZH_ALIASES,
            *_HALF_YEAR_EN_ALIASES,
        )
    )
)
_PERIOD_QUANTITY = (
    rf"[-−－]?\s*(?:\d+(?:[.．]\d+)?|{_ENGLISH_QUANTITY}|[{_CHINESE_NUMBER_CHARS}]+)"
)
# These patterns run only after the contract-owned matches.  They identify a
# period-shaped fragment that the grammar did not consume, including a
# missing/unknown prefix or unit, so the caller cannot silently treat it as
# an ordinary metric qualifier.
_PERIOD_QUANTITY_RESIDUE_PATTERN = re.compile(
    rf"(?<![a-z0-9]){_PERIOD_QUANTITY}\s*"
    rf"{_alternatives(_PERIOD_UNIT_ALIASES)}(?![a-z0-9])"
)
_RELATIVE_QUANTITY_RESIDUE_PATTERN = re.compile(
    rf"(?<![a-z0-9])(?:{_alternatives(_ALL_RELATIVE_EN_PREFIXES + (_THIS_EN_PREFIX,))})"
    rf"\s+{_PERIOD_QUANTITY}(?![a-z0-9])"
    rf"|(?<![a-z0-9])(?:{_alternatives(_CANDIDATE_ZH_PREFIXES)})"
    rf"\s*{_PERIOD_QUANTITY}(?![a-z0-9])"
)
# Known calendar units and to-date forms outside the supported contract still
# carry period intent even when no quantity is present.  Keep this vocabulary
# explicit so ordinary qualifiers such as ``last campaign`` are not mistaken
# for dates, while unsupported requests can never degrade to ``outcome=none``.
_UNSUPPORTED_EN_PERIOD_UNITS = ("quarter", "quarters", "fortnight", "fortnights")
_UNSUPPORTED_EN_TO_DATE_PERIODS = (
    "week to date",
    "month to date",
    "quarter to date",
    "year to date",
)
_UNSUPPORTED_ZH_PERIOD_UNITS = ("季", "季度")
_PERIOD_PUNCTUATION_SEPARATOR = r"(?:(?:[^\w\s]|_)+)"
_PUNCTUATED_PERIOD_QUANTITY_RESIDUE_PATTERN = re.compile(
    rf"(?<![a-z0-9])"
    rf"(?:\d+(?:[.．]\d+)?|{_ENGLISH_QUANTITY}|[{_CHINESE_NUMBER_CHARS}]+)"
    rf"\s*{_PERIOD_PUNCTUATION_SEPARATOR}\s*"
    rf"(?:{_alternatives(_PERIOD_UNIT_ALIASES + _UNSUPPORTED_EN_PERIOD_UNITS + _UNSUPPORTED_ZH_PERIOD_UNITS)})"
    rf"(?![a-z0-9])"
)
_PUNCTUATED_RELATIVE_PERIOD_PATTERN = re.compile(
    rf"(?<![a-z0-9])"
    rf"(?:{_alternatives(_ALL_RELATIVE_EN_PREFIXES + (_THIS_EN_PREFIX,))})"
    rf"\s*{_PERIOD_PUNCTUATION_SEPARATOR}\s*"
    rf"(?:\d+|{_ENGLISH_QUANTITY})"
    rf"\s*{_PERIOD_PUNCTUATION_SEPARATOR}\s*"
    rf"[a-z]+(?:\s*{_PERIOD_PUNCTUATION_SEPARATOR}\s*[a-z]+)*"
    rf"(?![a-z0-9])"
)
_UNSUPPORTED_PERIOD_RESIDUE_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![a-z0-9])(?:{_alternatives(_ALL_RELATIVE_EN_PREFIXES + (_THIS_EN_PREFIX,))})"
    rf"\s+(?:{_alternatives(_UNSUPPORTED_EN_PERIOD_UNITS)})(?![a-z0-9])"
    rf"|(?<![a-z0-9])(?:{_alternatives(_UNSUPPORTED_EN_TO_DATE_PERIODS)})(?![a-z0-9])"
    rf"|(?:{_alternatives(_CANDIDATE_ZH_PREFIXES)})\s*"
    rf"(?:{_alternatives(_UNSUPPORTED_ZH_PERIOD_UNITS)})"
    rf")"
)

_GROUPING_PATTERN = re.compile(
    rf"(?:"
    rf"(?:{_alternatives(GROUPING_QUALIFIER_PHRASES)})"
    rf"|按\s*(?:日|天|週|周|星期|月|年)(?:\s*group(?:ing)?)?"
    rf"|group(?:ing)?\s+by\s+(?:day|week|month|year)s?"
    rf")",
)

_FILTER_VALUE_LABELS = (
    "landing page",
    "page path",
    "campaign",
    "source",
    "medium",
    "filter",
    "page",
    "活動",
    "來源",
    "媒介",
    "篩選值",
    "页面",
    "頁面",
)
_CUSTOMER_QUALIFIER_LABELS = ("customer", "client", "account", "tenant")
_CHINESE_CUSTOMER_QUALIFIER_LABELS = ("客戶", "帳戶", "租戶")
_FILTER_VALUE = (
    r'(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|“[^”\r\n]*”|‘[^’\r\n]*’'
    r"|「[^」\r\n]*」|『[^』\r\n]*』|[^\s,，;；]+)"
)
_FILTER_VALUE_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![a-z0-9])(?:for\s+|針對\s*){_alternatives(_FILTER_VALUE_LABELS)}(?![a-z0-9])"
    rf"\s*(?:(?:is|equals?)\s+|(?:是|為|为)\s*|[:：=]\s*)?"
    rf"|(?<![a-z0-9]){_alternatives(_FILTER_VALUE_LABELS)}(?![a-z0-9])"
    rf"\s*(?:(?:is|equals?)\s+|(?:是|為|为)\s*|[:：=]\s*)"
    rf")(?P<value>{_FILTER_VALUE})"
)
_CUSTOMER_QUALIFIER_VALUE_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![a-z0-9])for\s+{_alternatives(_CUSTOMER_QUALIFIER_LABELS)}(?![a-z0-9])"
    rf"\s*(?:(?:is|equals?)\s+|[:：=]\s*)?"
    rf"|(?<![a-z0-9]){_alternatives(_CUSTOMER_QUALIFIER_LABELS)}(?![a-z0-9])"
    rf"\s*(?:(?:is|equals?)\s+|[:：=]\s*)"
    rf"|{_alternatives(_CHINESE_CUSTOMER_QUALIFIER_LABELS)}(?:名稱)?"
    rf"\s*(?:(?:是|為|为)\s*|[:：=]\s*)"
    rf")(?P<value>{_FILTER_VALUE})"
)
_CUSTOMER_QUALIFIER_CLAUSE_END = (
    r"[,，。；;]+|"
    r"(?<![a-z])(?:and|but|or|plus|then|with|versus|vs\.?|to|against)(?![a-z])|"
    r"(?<![a-z])(?:compared\s+(?:to|with)|in\s+comparison\s+(?:to|with))(?![a-z])|"
    r"(?:以及|並且|同時|加上|然後|再查|或者|或|相較於|相較|相比於|相比|對比|(?<!參)與)|"
    r"\s+[和跟]\s+"
)
_BARE_CUSTOMER_QUALIFIER_PATTERN = re.compile(
    rf"(?<![a-z0-9])for\s+(?P<value>[a-z0-9][a-z0-9 ._-]*?)"
    rf"(?=\s*(?:{_CUSTOMER_QUALIFIER_CLAUSE_END})|\s*$)"
)
_REVERSED_FILTER_VALUE_PATTERN = re.compile(
    rf"(?<![a-z0-9])from\s+(?P<value>{_FILTER_VALUE})\s+"
    rf"{_alternatives(_FILTER_VALUE_LABELS)}(?![a-z0-9])"
)
_QUERY_GROUPING_SUFFIX_PATTERN = re.compile(
    r"(?:"
    r"(?:(?:breakdown|group|grouping)\s+)?by\s+[a-z0-9][a-z0-9 _-]*"
    r"|按\s*[\u3400-\u9fff0-9a-z _-]+"
    r")"
)


def _is_supported_period_expression(value: str) -> bool:
    candidate = value.strip()
    patterns = (
        _FIXED_PATTERN,
        _DATE_RANGE_PATTERN,
        _DATE_SINGLE_PATTERN,
        *(rule.pattern for rule in _RELATIVE_RULES),
    )
    for pattern in patterns:
        match = pattern.match(candidate)
        if match is None or match.start() != 0:
            continue
        suffix = candidate[match.end() :].strip()
        if not suffix:
            return True
        if (
            _GROUPING_PATTERN.fullmatch(suffix) is not None
            or _QUERY_GROUPING_SUFFIX_PATTERN.fullmatch(suffix) is not None
        ):
            return True
    return False


def _protected_filter_value_spans(text: str) -> list[tuple[int, int]]:
    """Return explicit GA4 filter values that period parsing must not consume."""

    spans = [match.span("value") for match in _FILTER_VALUE_PATTERN.finditer(text)]
    spans.extend(
        match.span("value")
        for match in _CUSTOMER_QUALIFIER_VALUE_PATTERN.finditer(text)
    )
    for match in _BARE_CUSTOMER_QUALIFIER_PATTERN.finditer(text):
        value = match.group("value")
        relative_spans = [
            relative.span()
            for relative in _PUNCTUATED_RELATIVE_PERIOD_PATTERN.finditer(value)
        ]
        value_start = match.start("value")
        for residue in _PUNCTUATED_PERIOD_QUANTITY_RESIDUE_PATTERN.finditer(value):
            if _match_is_covered(residue.span(), relative_spans):
                continue
            suffix = value[residue.end() :]
            if suffix.strip() and not _is_supported_period_expression(suffix):
                continue
            spans.append((value_start + residue.start(), value_start + residue.end()))
    spans.extend(
        match.span("value") for match in _REVERSED_FILTER_VALUE_PATTERN.finditer(text)
    )
    return sorted(set(spans))


def strip_non_period_filter_values(text: str) -> str:
    """Remove explicit filter values while retaining their GA4 dimension labels."""

    result = text
    for start, end in reversed(_protected_filter_value_spans(text)):
        result = f"{result[:start]} {result[end:]}"
    return result


def _match_is_covered(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in spans)


_PERIOD_RANGE_CONNECTOR_PATTERN = re.compile(
    rf"\s*(?:{_alternatives(EXPLICIT_DATE_RANGE_SEPARATORS)})\s*"
)
_PERIOD_RANGE_CONNECTOR_SUFFIX_PATTERN = re.compile(
    rf"(?<![a-z0-9])(?:{_alternatives(EXPLICIT_DATE_RANGE_SEPARATORS)})(?![a-z0-9])"
    rf"(?:\s+(?:the\s+)?(?:date|dates))?\s*$"
)
_PERIOD_RANGE_LEADING_CONTEXT_PATTERN = re.compile(
    rf"(?:"
    rf"(?<![a-z0-9])(?:from|between)(?![a-z0-9])"
    rf"|{_alternatives(('自', '從', '从'))}"
    rf")\s*$"
)
_DANGLING_RANGE_TRAILING_SEPARATORS = tuple(
    separator
    for separator in EXPLICIT_DATE_RANGE_SEPARATORS
    if separator not in {"-", "－", "–", "—"}
)
_PERIOD_RANGE_TRAILING_CONTEXT_PATTERN = re.compile(
    rf"^\s*(?:{_alternatives(_DANGLING_RANGE_TRAILING_SEPARATORS)})(?![a-z0-9])"
)
_PERIOD_RANGE_WORD_CONNECTORS = tuple(
    separator
    for separator in EXPLICIT_DATE_RANGE_SEPARATORS
    if separator in {"to", "through", "到", "至"}
)
_PERIOD_RANGE_PUNCTUATION_CONNECTORS = tuple(
    separator
    for separator in EXPLICIT_DATE_RANGE_SEPARATORS
    if separator not in _PERIOD_RANGE_WORD_CONNECTORS
)
_PERIOD_RANGE_CONNECTOR_SIGNAL_PATTERN = re.compile(
    rf"(?<![a-z0-9])(?:{_alternatives(_PERIOD_RANGE_WORD_CONNECTORS)})(?![a-z0-9])"
    rf"|{_alternatives(_PERIOD_RANGE_PUNCTUATION_CONNECTORS)}"
)
# These are known date-range words that are intentionally outside the
# supported contract.  They are only treated as connectors when they are
# structurally adjacent to a period signal; ordinary prose is left alone.
_UNSUPPORTED_PERIOD_RANGE_CONNECTORS = (
    "until",
    "before",
    "after",
    "since",
    "截至",
    "之前",
    "以前",
    "之後",
    "以後",
)
_UNSUPPORTED_PERIOD_RANGE_CONNECTOR_PATTERN = re.compile(
    rf"(?<![a-z0-9])(?:{_alternatives(_UNSUPPORTED_PERIOD_RANGE_CONNECTORS)})(?![a-z0-9])"
)
_PERIOD_RANGE_PREFIX_SIGNAL_PATTERN = re.compile(
    rf"(?<![a-z0-9])(?:from|between)(?![a-z0-9])"
    rf"|{_alternatives(('自', '從', '从'))}"
)


def _is_independent_period_joiner(value: str) -> bool:
    return (
        _DATE_PERIOD_JOINER_PATTERN.fullmatch(value) is not None
        and _PERIOD_RANGE_CONNECTOR_SUFFIX_PATTERN.search(value) is None
    )


def _is_period_range_connector(value: str) -> bool:
    return _PERIOD_RANGE_CONNECTOR_PATTERN.fullmatch(value) is not None


def _has_period_range_leading_context(
    text: str,
    endpoint: PeriodPhraseMatch,
) -> bool:
    """Return whether an endpoint is introduced as the start of a range."""

    return (
        _PERIOD_RANGE_LEADING_CONTEXT_PATTERN.search(text[: endpoint.span[0]])
        is not None
    )


def _dangling_range_context_match(
    text: str,
    endpoint: PeriodPhraseMatch,
) -> PeriodPhraseMatch | None:
    """Reject range context that is missing a required endpoint."""

    if endpoint.start_date is None or endpoint.end_date is None:
        return None

    is_single_endpoint = (
        endpoint.start_date == endpoint.end_date
        and _DATE_RANGE_PATTERN.fullmatch(endpoint.phrase) is None
    )
    leading = (
        _PERIOD_RANGE_LEADING_CONTEXT_PATTERN.search(text[: endpoint.span[0]])
        if is_single_endpoint
        else None
    )
    trailing = _PERIOD_RANGE_TRAILING_CONTEXT_PATTERN.match(text[endpoint.span[1] :])
    if leading is None and trailing is None:
        return None

    start = leading.start() if leading is not None else endpoint.span[0]
    end = (
        endpoint.span[1] + trailing.end() if trailing is not None else endpoint.span[1]
    )
    return PeriodPhraseMatch(
        phrase=text[start:end].strip(),
        outcome="needs_clarification",
        span=(start, end),
        window_kind="explicit_date",
        reason_code="incomplete_date_range",
        message="日期範圍只有一個端點；請提供完整的起訖日期。",
    )


def _resolved_endpoint_matches(
    matches: list[PeriodPhraseMatch],
) -> list[PeriodPhraseMatch]:
    return sorted(
        (
            match
            for match in matches
            if match.outcome == "resolved"
            and match.start_date is not None
            and match.end_date is not None
        ),
        key=lambda match: match.span,
    )


def _malformed_range_tail_matches(
    text: str,
    matches: list[PeriodPhraseMatch],
    occupied: list[tuple[int, int]],
) -> tuple[list[PeriodPhraseMatch], list[tuple[int, int]]]:
    """Reject a malformed date following a range connector and parsed atom."""

    malformed: list[PeriodPhraseMatch] = []
    for endpoint in _resolved_endpoint_matches(matches):
        candidate = _DATE_RANGE_TAIL_CANDIDATE_PATTERN.match(text, endpoint.span[1])
        if candidate is None:
            continue
        span = candidate.span("candidate")
        if _match_is_covered(span, occupied):
            continue
        occupied.append(span)
        malformed.append(
            PeriodPhraseMatch(
                phrase=candidate.group("candidate"),
                outcome="invalid_period",
                span=span,
                window_kind="explicit_date",
                reason_code="invalid_date_format",
                message="明確日期必須是有效的 YYYY-MM-DD 日期。",
            )
        )
    return malformed, occupied


def _normalize_period_connectors(
    text: str,
    matches: list[PeriodPhraseMatch],
    *,
    policy: Any,
    anchor: date,
) -> tuple[list[PeriodInterval], list[PeriodPhraseMatch]]:
    """Assemble parsed period atoms using one connector grammar.

    Regex rules recognize atoms only.  This pass owns the relationship between
    every pair of adjacent resolved atoms, regardless of whether an endpoint
    came from an ISO date, a fixed phrase such as ``today``, or a relative
    phrase.  Therefore an unknown connector can never degrade a range into a
    union of unrelated endpoint days.
    """

    endpoints = _resolved_endpoint_matches(matches)
    endpoint_ids = {id(match) for match in endpoints}
    assembled_matches = [match for match in matches if id(match) not in endpoint_ids]
    assembled_intervals: list[PeriodInterval] = []
    groups: list[list[PeriodPhraseMatch]] = []
    for endpoint in endpoints:
        if not groups:
            groups.append([endpoint])
            continue
        previous = groups[-1][-1]
        connector = text[previous.span[1] : endpoint.span[0]]
        if _is_independent_period_joiner(connector) and not (
            len(groups[-1]) == 1
            and _has_period_range_leading_context(text, groups[-1][0])
        ):
            groups.append([endpoint])
        else:
            groups[-1].append(endpoint)

    for group in groups:
        if len(group) == 1:
            endpoint = group[0]
            assert endpoint.start_date is not None
            assert endpoint.end_date is not None
            dangling_range = _dangling_range_context_match(text, endpoint)
            if dangling_range is not None:
                assembled_matches.append(dangling_range)
                continue
            assembled_matches.append(endpoint)
            assembled_intervals.append(
                PeriodInterval(
                    phrase=endpoint.phrase,
                    window_kind=endpoint.window_kind or "explicit_date",
                    start_date=endpoint.start_date,
                    end_date=endpoint.end_date,
                )
            )
            continue

        first = group[0]
        last = group[-1]
        assert first.start_date is not None
        assert first.end_date is not None
        assert last.start_date is not None
        assert last.end_date is not None
        combined_span = (first.span[0], last.span[1])
        combined_phrase = text[combined_span[0] : combined_span[1]].strip()
        connector = text[first.span[1] : last.span[0]]
        if (
            len(group) == 2
            and _is_period_range_connector(connector)
            and first.start_date == first.end_date
            and last.start_date == last.end_date
        ):
            start = first.start_date
            end = last.end_date
            error = _date_error(start, end, policy=policy, today=anchor)
            if error is None:
                assembled_matches.append(
                    PeriodPhraseMatch(
                        phrase=combined_phrase,
                        outcome="resolved",
                        span=combined_span,
                        window_kind="explicit_date",
                        start_date=start,
                        end_date=end,
                    )
                )
                assembled_intervals.append(
                    PeriodInterval(
                        phrase=combined_phrase,
                        window_kind="explicit_date",
                        start_date=start,
                        end_date=end,
                    )
                )
            else:
                reason_code, message = error
                assembled_matches.append(
                    PeriodPhraseMatch(
                        phrase=combined_phrase,
                        outcome="invalid_period",
                        span=combined_span,
                        window_kind="explicit_date",
                        start_date=start,
                        end_date=end,
                        reason_code=reason_code,
                        message=message,
                    )
                )
            continue

        connector_text = connector.strip() or "(無連接文字)"
        assembled_matches.append(
            PeriodPhraseMatch(
                phrase=combined_phrase,
                outcome="needs_clarification",
                span=combined_span,
                window_kind="explicit_date",
                reason_code="unsupported_date_range_connector",
                message=(
                    f"期間之間的連接文字 {connector_text!r} 不在支援的日期 contract "
                    "語法中；請使用明確的 range separator，或分開說明各個期間。"
                ),
            )
        )

    return assembled_intervals, assembled_matches


def _period_residue_matches(
    text: str,
    occupied: list[tuple[int, int]],
) -> tuple[list[PeriodPhraseMatch], list[tuple[int, int]]]:
    """Return period-shaped fragments left unconsumed by the contract parser."""

    matches: list[PeriodPhraseMatch] = []
    for pattern, force_clarification in (
        (_PUNCTUATED_RELATIVE_PERIOD_PATTERN, True),
        (_PUNCTUATED_PERIOD_QUANTITY_RESIDUE_PATTERN, True),
        (_PERIOD_QUANTITY_RESIDUE_PATTERN, False),
        (_RELATIVE_QUANTITY_RESIDUE_PATTERN, False),
        (_UNSUPPORTED_PERIOD_RESIDUE_PATTERN, False),
    ):
        for match in pattern.finditer(text):
            span = match.span()
            if _match_is_covered(span, occupied):
                continue
            phrase = match.group(0).strip()
            occupied.append(span)
            if force_clarification:
                outcome, reason_code, message = (
                    "needs_clarification",
                    "ambiguous_period",
                    "期間詞使用未支援的標點分隔格式，請提供 contract 支援的期間語法。",
                )
            else:
                outcome, reason_code, message = _invalid_candidate_outcome(phrase)
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome=outcome,
                    span=span,
                    reason_code=reason_code,
                    message=message,
                )
            )
    return matches, occupied


def _date_error(
    start: date,
    end: date,
    *,
    policy: Any,
    today: date,
) -> tuple[str, str] | None:
    earliest_date = _policy_value(policy, "earliest_date", DEFAULT_EARLIEST_DATE)
    if start > end:
        return "invalid_date_range", "日期起點不得晚於終點。"
    if start < earliest_date:
        return (
            "date_before_available_range",
            f"目前只能查詢 {earliest_date.isoformat()} 之後的資料。",
        )
    if end > today:
        return "future_date_not_allowed", "日期終點不得晚於今天。"
    return None


def _relative_match_quantity(match: re.Match[str]) -> int | None:
    for name in ("zh_quantity", "en_quantity"):
        value = match.groupdict().get(name)
        if value is not None:
            return parse_quantity(value)
    return None


def _relative_match_span(match: re.Match[str]) -> tuple[int, int]:
    return match.span()


def _fixed_dates(
    window_kind: str,
    anchor: date,
    canonical_phrase: str | None = None,
) -> tuple[date, date]:
    if window_kind == "single_day":
        if canonical_phrase == "yesterday":
            yesterday = anchor - timedelta(days=1)
            return yesterday, yesterday
        return anchor, anchor
    if window_kind == "week_to_date":
        return anchor - timedelta(days=anchor.weekday()), anchor
    if window_kind == "completed_weeks":
        return _relative_dates(window_kind, 1, anchor)
    if window_kind == "previous_days":
        return _relative_dates(window_kind, 1, anchor)
    if window_kind == "month_to_date":
        return anchor.replace(day=1), anchor
    if window_kind == "completed_months":
        return _relative_dates(window_kind, 1, anchor)
    if window_kind == "year_to_date":
        return anchor.replace(month=1, day=1), anchor
    if window_kind == "completed_years":
        return _relative_dates(window_kind, 1, anchor)
    raise ValueError(f"Unsupported fixed window kind: {window_kind}")


def _invalid_candidate_outcome(phrase: str) -> tuple[str, str, str]:
    normalized = _normalize(phrase)
    if re.search(r"(?:^|\s|[-−])(?:0|零|〇|○|zero)(?:\s|$)", normalized) or re.search(
        r"(?:負|负|negative|[-−])\s*(?:\d|[零〇○一二兩两三四五六七八九十百千万萬億亿]|one|two|three|four|five|six|seven|eight|nine|ten)",
        normalized,
    ):
        return (
            "invalid_period",
            "invalid_period_quantity",
            "期間數量必須是正整數，不能使用零或負數。",
        )
    if re.search(r"(?:本|這|this)\s*(?:\d|[一二兩两三四五六七八九十])", normalized):
        return (
            "invalid_period",
            "invalid_period_combination",
            "這個期間詞的數量與 window kind 組合不合法。",
        )
    if re.search(r"\d+[.]\d+", normalized):
        return (
            "invalid_period",
            "fractional_period_quantity",
            "期間數量必須是正整數，不支援小數。",
        )
    return (
        "needs_clarification",
        "ambiguous_period",
        "無法唯一判斷期間的數量或 window kind，請提供明確的日期範圍。",
    )


def _explicit_date_matches(
    text: str,
    *,
    policy: Any,
    anchor: date,
) -> tuple[list[PeriodInterval], list[PeriodPhraseMatch], list[tuple[int, int]]]:
    intervals: list[PeriodInterval] = []
    matches: list[PeriodPhraseMatch] = []
    occupied = _protected_filter_value_spans(text)

    for match in _DATE_RANGE_PATTERN.finditer(text):
        span = match.span()
        if _match_is_covered(span, occupied):
            continue
        start_text = match.group("start")
        end_text = match.group("end")
        phrase = match.group(0).strip()
        occupied.append(span)
        if not _ISO_DATE_PATTERN.fullmatch(
            start_text
        ) or not _ISO_DATE_PATTERN.fullmatch(end_text):
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome="invalid_period",
                    span=span,
                    window_kind="explicit_date",
                    reason_code="invalid_date_format",
                    message="明確日期必須使用 YYYY-MM-DD 格式；斜線日期不支援。",
                )
            )
            continue
        try:
            start = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text)
        except ValueError:
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome="invalid_period",
                    span=span,
                    window_kind="explicit_date",
                    reason_code="invalid_date_format",
                    message="明確日期必須是有效的 YYYY-MM-DD 日期。",
                )
            )
            continue
        error = _date_error(start, end, policy=policy, today=anchor)
        if error is not None:
            reason_code, message = error
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome="invalid_period",
                    span=span,
                    window_kind="explicit_date",
                    start_date=start,
                    end_date=end,
                    reason_code=reason_code,
                    message=message,
                )
            )
            continue
        intervals.append(
            PeriodInterval(
                phrase=phrase,
                window_kind="explicit_date",
                start_date=start,
                end_date=end,
            )
        )
        matches.append(
            PeriodPhraseMatch(
                phrase=phrase,
                outcome="resolved",
                span=span,
                window_kind="explicit_date",
                start_date=start,
                end_date=end,
            )
        )

    for match in _DATE_SINGLE_PATTERN.finditer(text):
        span = match.span()
        if _match_is_covered(span, occupied):
            continue
        phrase = match.group(0)
        occupied.append(span)
        if not _ISO_DATE_PATTERN.fullmatch(phrase):
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome="invalid_period",
                    span=span,
                    window_kind="explicit_date",
                    reason_code="invalid_date_format",
                    message="明確日期必須使用 YYYY-MM-DD 格式；斜線日期不支援。",
                )
            )
            continue
        try:
            parsed = date.fromisoformat(phrase)
        except ValueError:
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome="invalid_period",
                    span=span,
                    window_kind="explicit_date",
                    reason_code="invalid_date_format",
                    message="明確日期必須是有效的 YYYY-MM-DD 日期。",
                )
            )
            continue
        error = _date_error(parsed, parsed, policy=policy, today=anchor)
        if error is not None:
            reason_code, message = error
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome="invalid_period",
                    span=span,
                    window_kind="explicit_date",
                    start_date=parsed,
                    end_date=parsed,
                    reason_code=reason_code,
                    message=message,
                )
            )
            continue
        intervals.append(
            PeriodInterval(
                phrase=phrase,
                window_kind="explicit_date",
                start_date=parsed,
                end_date=parsed,
            )
        )
        matches.append(
            PeriodPhraseMatch(
                phrase=phrase,
                outcome="resolved",
                span=span,
                window_kind="explicit_date",
                start_date=parsed,
                end_date=parsed,
            )
        )

    candidate_spans = [
        (match.span(), match.group(0))
        for match in _DATE_LIKE_CANDIDATE_PATTERN.finditer(text)
    ]
    candidate_spans.extend(
        (match.span("candidate"), match.group("candidate"))
        for match in _DATE_CONTEXT_CANDIDATE_PATTERN.finditer(text)
    )
    for span, phrase in sorted(candidate_spans):
        if _match_is_covered(span, occupied):
            continue
        occupied.append(span)
        matches.append(
            PeriodPhraseMatch(
                phrase=phrase,
                outcome="invalid_period",
                span=span,
                window_kind="explicit_date",
                reason_code="invalid_date_format",
                message=(
                    "明確日期 token 必須完整使用 YYYY-MM-DD 格式，"
                    "不得附帶額外數字或英數字元。"
                ),
            )
        )
    return intervals, matches, occupied


def explicit_date_range_separator_spans(
    text: str,
) -> tuple[tuple[int, int], ...]:
    """Return separator spans inside lexically recognized period ranges.

    ``text`` must be the same normalized string whose clauses will be split;
    callers use the coordinates to protect ``to``/equivalent separators from
    generic mixed-request splitting.
    """

    atom_spans: list[tuple[int, int]] = [
        match.span() for match in _DATE_SINGLE_PATTERN.finditer(text)
    ]
    atom_spans.extend(match.span() for match in _FIXED_PATTERN.finditer(text))
    for rule in _RELATIVE_RULES:
        atom_spans.extend(match.span() for match in rule.pattern.finditer(text))
    atom_spans.sort()

    separator_spans: list[tuple[int, int]] = []
    for current, following in zip(atom_spans, atom_spans[1:]):
        if current[1] > following[0]:
            continue
        connector = text[current[1] : following[0]]
        if _is_period_range_connector(connector):
            separator_spans.append((current[1], following[0]))
    return tuple(separator_spans)


def is_period_range_connector(value: str) -> bool:
    """Return whether ``value`` is a connector reserved for period ranges."""

    return _is_period_range_connector(_normalize(value))


def _union_days(intervals: tuple[PeriodInterval, ...] | list[PeriodInterval]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals, key=lambda item: (item.start_date, item.end_date))
    start = ordered[0].start_date
    end = ordered[0].end_date
    total = 0
    for interval in ordered[1:]:
        if interval.start_date <= end + timedelta(days=1):
            end = max(end, interval.end_date)
            continue
        total += (end - start).days + 1
        start, end = interval.start_date, interval.end_date
    return total + (end - start).days + 1


def _new_match_from_interval(
    phrase: str,
    span: tuple[int, int],
    window_kind: str,
    *,
    policy: Any,
    anchor: date,
    quantity: int,
) -> tuple[PeriodInterval | None, PeriodPhraseMatch]:
    if quantity <= 0:
        return None, PeriodPhraseMatch(
            phrase=phrase,
            outcome="invalid_period",
            span=span,
            window_kind=window_kind,
            reason_code="invalid_period_quantity",
            message="期間數量必須是正整數，不能使用零或負數。",
        )
    try:
        start, end = _relative_dates(window_kind, quantity, anchor)
    except (OverflowError, ValueError):
        max_days = _policy_value(
            policy,
            "max_date_range_days",
            DEFAULT_MAX_DATE_RANGE_DAYS,
        )
        return None, PeriodPhraseMatch(
            phrase=phrase,
            outcome="invalid_period",
            span=span,
            window_kind=window_kind,
            reason_code="period_quantity_out_of_range",
            message=(
                f"期間數量超出可處理日期範圍，請提供不超過 {max_days} "
                "個 calendar days 的期間。"
            ),
        )
    error = _date_error(start, end, policy=policy, today=anchor)
    if error is not None:
        reason_code, message = error
        return None, PeriodPhraseMatch(
            phrase=phrase,
            outcome="invalid_period",
            span=span,
            window_kind=window_kind,
            start_date=start,
            end_date=end,
            reason_code=reason_code,
            message=message,
        )
    interval = PeriodInterval(
        phrase=phrase,
        window_kind=window_kind,
        start_date=start,
        end_date=end,
    )
    return interval, PeriodPhraseMatch(
        phrase=phrase,
        outcome="resolved",
        span=span,
        window_kind=window_kind,
        start_date=start,
        end_date=end,
    )


def _implicit_previous_period(
    current: PeriodInterval,
    *,
    policy: Any,
) -> tuple[PeriodInterval | None, tuple[str, str] | None]:
    day_count = current.days
    end = current.start_date - timedelta(days=1)
    start = end - timedelta(days=day_count - 1)
    earliest_date = _policy_value(policy, "earliest_date", DEFAULT_EARLIEST_DATE)
    if start < earliest_date:
        return None, (
            "date_before_available_range",
            f"traffic summary 的前期需要從 {start.isoformat()} 開始，但目前最早可查日期為 {earliest_date.isoformat()}。",
        )
    return (
        PeriodInterval(
            phrase="previous period",
            window_kind="fixed_previous_comparison",
            start_date=start,
            end_date=end,
            source="implicit",
        ),
        None,
    )


class PeriodSafetyAudit:
    """Fail closed when period-shaped input escaped atom recognition.

    This is a postcondition check, not another period parser.  It uses the
    spans already produced by protected-value masking and atom recognition,
    then checks the small set of contract-owned date/period shapes that can be
    left behind by a truncated or malformed token.  Range checks use the
    resolved atoms and those same residual shapes as endpoints.
    """

    def __init__(
        self,
        *,
        text: str,
        protected_spans: tuple[tuple[int, int], ...] | list[tuple[int, int]],
        recognized_spans: tuple[tuple[int, int], ...] | list[tuple[int, int]],
        phrase_matches: tuple[PeriodPhraseMatch, ...] | list[PeriodPhraseMatch],
    ):
        self.text = text
        self.protected_spans = tuple(protected_spans)
        self.recognized_spans = tuple(recognized_spans)
        self.phrase_matches = tuple(phrase_matches)

    @staticmethod
    def _covered(
        span: tuple[int, int],
        spans: tuple[tuple[int, int], ...] | list[tuple[int, int]],
    ) -> bool:
        return any(start <= span[0] and span[1] <= end for start, end in spans)

    @staticmethod
    def _overlaps(
        first: tuple[int, int],
        second: tuple[int, int],
    ) -> bool:
        return first[0] < second[1] and second[0] < first[1]

    def _explained_spans(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            {
                *self.protected_spans,
                *self.recognized_spans,
                *(match.span for match in self.phrase_matches),
            }
        )

    def _unexplained_period_signals(
        self,
        explained_spans: tuple[tuple[int, int], ...],
    ) -> list[PeriodPhraseMatch]:
        # The ordinary residue pass already owns the contract's quantity,
        # unit, punctuation, and unsupported-vocabulary signals.  Reuse it
        # here as the postcondition instead of maintaining a second copy of
        # those rules.
        probe_spans = list(explained_spans)
        residue_matches, _ = _period_residue_matches(self.text, probe_spans)
        raw_matches = list(residue_matches)

        # Recognition intentionally accepts only complete ISO dates.  The
        # partial forms below are the one date-shaped gap that can remain
        # after the normal candidate pass (for example ``date 2026-09``).
        for pattern in (
            _DATE_CONTEXT_PARTIAL_CANDIDATE_PATTERN,
            _DATE_PARTIAL_CANDIDATE_PATTERN,
        ):
            for match in pattern.finditer(self.text):
                span = match.span()
                candidate_span = (
                    match.span("candidate")
                    if "candidate" in match.groupdict()
                    else span
                )
                if self._covered(span, explained_spans) or self._covered(
                    candidate_span,
                    explained_spans,
                ):
                    continue
                raw_matches.append(
                    PeriodPhraseMatch(
                        phrase=match.group(0).strip(),
                        outcome="invalid_period",
                        span=span,
                        window_kind="explicit_date",
                        reason_code="invalid_date_format",
                        message=(
                            "明確日期 token 必須完整使用 YYYY-MM-DD 格式，"
                            "不得省略日期元件或附帶額外字元。"
                        ),
                    )
                )

        # Prefer the most specific/complete shape when patterns overlap, for
        # example the contextual ``date 2026-09`` over its numeric suffix.
        accepted: list[PeriodPhraseMatch] = []
        for candidate in sorted(
            raw_matches,
            key=lambda item: (item.span[0], -(item.span[1] - item.span[0])),
        ):
            if any(
                self._covered(candidate.span, (existing.span,)) for existing in accepted
            ):
                continue
            accepted.append(candidate)
        return accepted

    def _resolved_endpoints(self) -> list[PeriodPhraseMatch]:
        return _resolved_endpoint_matches(list(self.phrase_matches))

    @staticmethod
    def _is_complete_resolved_range(match: PeriodPhraseMatch) -> bool:
        if match.outcome != "resolved":
            return False
        phrase = match.phrase.strip()
        if _DATE_RANGE_PATTERN.fullmatch(phrase) is not None:
            return True
        return (
            re.search(
                r"(?<![a-z0-9])(?:to|through)(?![a-z0-9])|[~～－–—]",
                phrase,
            )
            is not None
        )

    def _adjacent_before(
        self,
        position: int,
        candidates: list[PeriodPhraseMatch],
    ) -> PeriodPhraseMatch | None:
        for candidate in sorted(
            candidates, key=lambda item: item.span[1], reverse=True
        ):
            if (
                candidate.span[1] <= position
                and not self.text[candidate.span[1] : position].strip()
            ):
                return candidate
        return None

    def _adjacent_after(
        self,
        position: int,
        candidates: list[PeriodPhraseMatch],
    ) -> PeriodPhraseMatch | None:
        for candidate in sorted(candidates, key=lambda item: item.span[0]):
            if (
                candidate.span[0] >= position
                and not self.text[position : candidate.span[0]].strip()
            ):
                return candidate
        return None

    def _finding(
        self,
        *,
        span: tuple[int, int],
        reason_code: str,
        message: str,
    ) -> PeriodPhraseMatch:
        return PeriodPhraseMatch(
            phrase=self.text[span[0] : span[1]].strip(),
            outcome="needs_clarification",
            span=span,
            window_kind="explicit_date",
            reason_code=reason_code,
            message=message,
        )

    def _is_filter_attribution_connector(self, span: tuple[int, int]) -> bool:
        connector = self.text[span[0] : span[1]].casefold()
        if connector != "to":
            return False
        return (
            re.search(r"(?<![a-z0-9])attributed\s+$", self.text[: span[0]]) is not None
        )

    def _range_finding_for_connector(
        self,
        connector_span: tuple[int, int],
        *,
        connector_kind: str,
        explained_spans: tuple[tuple[int, int], ...],
        signal_matches: list[PeriodPhraseMatch],
    ) -> PeriodPhraseMatch | None:
        if self._covered(connector_span, explained_spans):
            return None
        if connector_kind == "supported" and self._covered(
            connector_span,
            tuple(match.span for match in signal_matches),
        ):
            # A dash inside ``180-day`` or a truncated date is part of the
            # signal itself, not a range connector.
            return None
        if self._is_filter_attribution_connector(connector_span):
            return None

        resolved = self._resolved_endpoints()
        period_candidates = [*resolved, *signal_matches]
        left = self._adjacent_before(connector_span[0], period_candidates)
        right = self._adjacent_after(connector_span[1], period_candidates)
        connector_text = self.text[connector_span[0] : connector_span[1]].strip()
        is_dash_like = connector_text in {"-", "－", "–", "—"}
        unsupported = connector_kind == "unsupported"
        reason_code = (
            "unsupported_date_range_connector"
            if unsupported
            else "incomplete_date_range"
        )
        if left is not None and right is not None:
            start = min(left.span[0], connector_span[0])
            end = max(right.span[1], connector_span[1])
            message = (
                f"期間之間的連接文字 {connector_text!r} 不在支援的日期 contract "
                "語法中；請提供兩個完整 endpoint。"
                if unsupported
                else "日期範圍的兩側都必須是完整 endpoint；請提供完整的起訖日期。"
            )
            return self._finding(
                span=(start, end),
                reason_code=reason_code,
                message=message,
            )

        if left is not None:
            suffix = self.text[connector_span[1] :].strip()
            if (
                is_dash_like
                and suffix
                and _QUERY_GROUPING_SUFFIX_PATTERN.fullmatch(suffix)
            ):
                return None
            start = left.span[0]
            end = connector_span[1]
            return self._finding(
                span=(start, end),
                reason_code=reason_code,
                message=(
                    f"期間之間的連接文字 {connector_text!r} 不在支援的日期 contract "
                    "語法中；請提供兩個完整 endpoint。"
                    if unsupported
                    else "日期範圍缺少終點；請提供完整的起訖日期。"
                ),
            )

        if right is not None:
            return self._finding(
                span=(connector_span[0], right.span[1]),
                reason_code=reason_code,
                message=(
                    f"期間之間的連接文字 {connector_text!r} 不在支援的日期 contract "
                    "語法中；請提供兩個完整 endpoint。"
                    if unsupported
                    else "日期範圍缺少起點；請提供完整的起訖日期。"
                ),
            )

        # A terminal range token is itself an unambiguous incomplete signal.
        # Internal punctuation remains ignored unless it is adjacent to a
        # recognized period atom or residual period shape.
        if not self.text[connector_span[1] :].strip():
            return self._finding(
                span=connector_span,
                reason_code=reason_code,
                message="日期範圍 connector 沒有連接完整 endpoint；請提供完整的起訖日期。",
            )
        return None

    def _range_findings(
        self,
        *,
        explained_spans: tuple[tuple[int, int], ...],
        signal_matches: list[PeriodPhraseMatch],
    ) -> list[PeriodPhraseMatch]:
        findings: list[PeriodPhraseMatch] = []
        for pattern, connector_kind in (
            (_PERIOD_RANGE_CONNECTOR_SIGNAL_PATTERN, "supported"),
            (_UNSUPPORTED_PERIOD_RANGE_CONNECTOR_PATTERN, "unsupported"),
        ):
            for match in pattern.finditer(self.text):
                finding = self._range_finding_for_connector(
                    match.span(),
                    connector_kind=connector_kind,
                    explained_spans=explained_spans,
                    signal_matches=signal_matches,
                )
                if finding is not None:
                    findings.append(finding)

        # Range prefixes (``from``/``between`` and their supported Chinese
        # forms) are context, not endpoints.  If one survives recognition it
        # must still have a complete endpoint pair.
        period_candidates = [*self._resolved_endpoints(), *signal_matches]
        for match in _PERIOD_RANGE_PREFIX_SIGNAL_PATTERN.finditer(self.text):
            prefix_span = match.span()
            if self._covered(prefix_span, explained_spans):
                continue
            right = self._adjacent_after(prefix_span[1], period_candidates)
            overlapping_right = next(
                (
                    candidate
                    for candidate in period_candidates
                    if candidate.span[0] <= prefix_span[1] < candidate.span[1]
                ),
                None,
            )
            right = overlapping_right or right
            if right is not None:
                if self._is_complete_resolved_range(right):
                    continue
                findings.append(
                    self._finding(
                        span=(prefix_span[0], right.span[1]),
                        reason_code="incomplete_date_range",
                        message="日期範圍缺少另一個 endpoint；請提供完整的起訖日期。",
                    )
                )
            elif not self.text[prefix_span[1] :].strip():
                findings.append(
                    self._finding(
                        span=prefix_span,
                        reason_code="incomplete_date_range",
                        message="日期範圍 prefix 沒有完整 endpoint；請提供完整的起訖日期。",
                    )
                )
        return findings

    def run(self) -> tuple[PeriodPhraseMatch, ...]:
        explained_spans = self._explained_spans()
        signal_matches = self._unexplained_period_signals(explained_spans)
        range_findings = self._range_findings(
            explained_spans=explained_spans,
            signal_matches=signal_matches,
        )

        findings: list[PeriodPhraseMatch] = []
        for finding in sorted(
            range_findings,
            key=lambda item: (item.span[0], -(item.span[1] - item.span[0])),
        ):
            if any(
                self._overlaps(finding.span, existing.span) for existing in findings
            ):
                continue
            findings.append(finding)

        for signal in signal_matches:
            if any(self._covered(signal.span, (finding.span,)) for finding in findings):
                continue
            if any(self._overlaps(signal.span, existing.span) for existing in findings):
                continue
            findings.append(signal)
        return tuple(sorted(findings, key=lambda item: item.span))


def resolve_period_intent(
    request: str,
    *,
    policy: Any = None,
    today: date | None = None,
    include_previous_comparison: bool = False,
) -> PeriodIntent:
    """Resolve all explicit periods in a request and compute their union.

    The parser never turns an unrecognized or malformed period phrase into a
    shorter request.  It returns a structured fallback instead.
    """

    if not isinstance(request, str):
        return PeriodIntent(
            outcome="needs_clarification",
            max_days=int(
                _policy_value(
                    policy,
                    "max_date_range_days",
                    DEFAULT_MAX_DATE_RANGE_DAYS,
                )
            ),
            reason_code="invalid_period",
            message="期間需求必須是文字，請提供明確日期範圍。",
        )
    text = _normalize(request)
    anchor = _today(policy, today)
    max_days = int(
        _policy_value(policy, "max_date_range_days", DEFAULT_MAX_DATE_RANGE_DAYS)
    )
    explicit_periods, matches, occupied = _explicit_date_matches(
        text,
        policy=policy,
        anchor=anchor,
    )

    for rule in _RELATIVE_RULES:
        for match in rule.pattern.finditer(text):
            span = _relative_match_span(match)
            if _match_is_covered(span, occupied):
                continue
            phrase = match.group(0).strip()
            occupied.append(span)
            quantity = rule.fixed_quantity or _relative_match_quantity(match)
            if quantity is None:
                outcome, reason_code, message = _invalid_candidate_outcome(phrase)
                matches.append(
                    PeriodPhraseMatch(
                        phrase=phrase,
                        outcome=outcome,
                        span=span,
                        window_kind=rule.window_kind,
                        reason_code=reason_code,
                        message=message,
                    )
                )
                continue
            interval, phrase_match = _new_match_from_interval(
                phrase,
                span,
                rule.window_kind,
                policy=policy,
                anchor=anchor,
                quantity=quantity,
            )
            matches.append(phrase_match)
            if interval is not None:
                explicit_periods.append(interval)

    for match in _FRACTIONAL_PERIOD_PATTERN.finditer(text):
        span = match.span()
        if _match_is_covered(span, occupied):
            continue
        phrase = match.group(0).strip()
        occupied.append(span)
        outcome, reason_code, message = _invalid_candidate_outcome(phrase)
        matches.append(
            PeriodPhraseMatch(
                phrase=phrase,
                outcome=outcome,
                span=span,
                reason_code=reason_code,
                message=message,
            )
        )

    for match in _FIXED_PATTERN.finditer(text):
        span = match.span()
        if _match_is_covered(span, occupied):
            continue
        phrase = match.group(0).strip()
        occupied.append(span)
        window_kind, _canonical = _FIXED_BY_PHRASE[phrase]
        start, end = _fixed_dates(window_kind, anchor, _canonical)
        error = _date_error(start, end, policy=policy, today=anchor)
        if error is not None:
            reason_code, message = error
            matches.append(
                PeriodPhraseMatch(
                    phrase=phrase,
                    outcome="invalid_period",
                    span=span,
                    window_kind=window_kind,
                    start_date=start,
                    end_date=end,
                    reason_code=reason_code,
                    message=message,
                )
            )
            continue
        explicit_periods.append(
            PeriodInterval(
                phrase=phrase,
                window_kind=window_kind,
                start_date=start,
                end_date=end,
            )
        )
        matches.append(
            PeriodPhraseMatch(
                phrase=phrase,
                outcome="resolved",
                span=span,
                window_kind=window_kind,
                start_date=start,
                end_date=end,
            )
        )

    malformed_tails, occupied = _malformed_range_tail_matches(
        text,
        matches,
        occupied,
    )
    matches.extend(malformed_tails)

    comparison_modifier = False
    for match in _COMPARISON_PATTERN.finditer(text):
        span = match.span()
        if _match_is_covered(span, occupied):
            continue
        comparison_modifier = True
        occupied.append(span)
        outcome = "resolved" if include_previous_comparison else "needs_clarification"
        matches.append(
            PeriodPhraseMatch(
                phrase=match.group(0).strip(),
                outcome=outcome,
                span=span,
                window_kind="fixed_previous_comparison",
                reason_code=(
                    None
                    if include_previous_comparison
                    else "unsupported_comparison_modifier"
                ),
                message=(
                    None
                    if include_previous_comparison
                    else (
                        "前期比較只由 traffic_summary 的固定 report contract 支援；"
                        "一般 GA4 metric 查詢請提供明確的 current date range。"
                    )
                ),
            )
        )

    for match in _PERIOD_CANDIDATE_PATTERN.finditer(text):
        span = match.span()
        if _match_is_covered(span, occupied):
            continue
        phrase = match.group(0).strip()
        outcome, reason_code, message = _invalid_candidate_outcome(phrase)
        occupied.append(span)
        matches.append(
            PeriodPhraseMatch(
                phrase=phrase,
                outcome=outcome,
                span=span,
                reason_code=reason_code,
                message=message,
            )
        )

    residue_matches, occupied = _period_residue_matches(text, occupied)
    matches.extend(residue_matches)

    explicit_periods, matches = _normalize_period_connectors(
        text,
        matches,
        policy=policy,
        anchor=anchor,
    )

    safety_findings = PeriodSafetyAudit(
        text=text,
        protected_spans=tuple(_protected_filter_value_spans(text)),
        recognized_spans=tuple(occupied),
        phrase_matches=tuple(matches),
    ).run()
    matches.extend(safety_findings)

    # A local valid atom cannot stand in for a request that also contains an
    # invalid, ambiguous, or otherwise unexplained period structure.  Clear
    # all explicit intervals before computing requested_days so callers never
    # receive a silently shortened intent.
    if any(match.outcome != "resolved" for match in matches):
        explicit_periods = []

    matches.sort(key=lambda item: item.span)
    invalid_matches = [match for match in matches if match.outcome == "invalid_period"]
    clarification_matches = [
        match for match in matches if match.outcome == "needs_clarification"
    ]
    requested_days = _union_days(explicit_periods)

    if invalid_matches:
        first = invalid_matches[0]
        return PeriodIntent(
            outcome="invalid_period",
            explicit_periods=tuple(explicit_periods),
            requested_days=requested_days,
            max_days=max_days,
            phrase_matches=tuple(matches),
            comparison_modifier=comparison_modifier,
            reason_code=first.reason_code or "invalid_period",
            message=first.message,
        )
    if clarification_matches:
        first = clarification_matches[0]
        return PeriodIntent(
            outcome="needs_clarification",
            explicit_periods=tuple(explicit_periods),
            requested_days=requested_days,
            max_days=max_days,
            phrase_matches=tuple(matches),
            comparison_modifier=comparison_modifier,
            reason_code=first.reason_code or "ambiguous_period",
            message=first.message,
        )

    outcome = "resolved" if matches else "none"
    intent = PeriodIntent(
        outcome=outcome,
        explicit_periods=tuple(explicit_periods),
        requested_days=requested_days,
        max_days=max_days,
        phrase_matches=tuple(matches),
        comparison_modifier=comparison_modifier,
    )
    if include_previous_comparison and len(intent.explicit_periods) == 1:
        implicit, error = _implicit_previous_period(
            intent.explicit_periods[0],
            policy=policy,
        )
        if error is not None:
            reason_code, message = error
            return replace(
                intent,
                outcome="invalid_period",
                explicit_periods=(),
                requested_days=0,
                implicit_periods=(),
                reason_code=reason_code,
                message=message,
            )
        assert implicit is not None
        intent = replace(intent, implicit_periods=(implicit,))
    return intent


def is_query_context_clause(
    clause: str,
    *,
    include_previous_comparison: bool = False,
) -> bool:
    """Return whether a mixed-request clause is period/grouping context."""

    normalized = _normalize(clause)
    if not normalized:
        return False
    if _GROUPING_PATTERN.fullmatch(normalized) is not None:
        return True
    period_intent = resolve_period_intent(
        normalized,
        include_previous_comparison=include_previous_comparison,
    )
    if period_intent.outcome != "resolved" or not period_intent.phrase_matches:
        return False

    # A clause such as "email open rate last week" contains a valid period,
    # but is still an analysis request.  Only treat a clause as context after
    # removing the contract-owned phrases and harmless date connectors.
    residue = normalized
    for match in sorted(
        period_intent.phrase_matches,
        key=lambda item: item.span[0],
        reverse=True,
    ):
        start, end = match.span
        residue = f"{residue[:start]} {residue[end:]}"
    residue = re.sub(
        r"(?<![a-z0-9])(?:from|to|through|and|the|for|date|dates|period|compare|compared|with)(?![a-z0-9])",
        " ",
        residue,
    )
    residue = re.sub(r"[，,。；;：:到至與和及以及、~～－–—-]", " ", residue)
    residue = residue.strip()
    return not residue


def analysis_ignored_english_tokens() -> set[str]:
    result = {
        "a",
        "an",
        "day",
        "days",
        "half",
        "last",
        "month",
        "months",
        "past",
        "previous",
        "recent",
        "through",
        "today",
        "week",
        "weeks",
        "year",
        "years",
        *(_ENGLISH_NUMBER_VALUES.keys()),
        "compare",
        "compared",
        "comparison",
        "with",
        "the",
        "period",
        "group",
        "grouping",
        "by",
        "daily",
        "weekly",
        "monthly",
        "yearly",
    }
    for family in PHRASE_FAMILIES:
        aliases = _contract_aliases(family)
        for alias in (
            *aliases.get("en_prefixes", ()),
            *aliases.get("en_units", ()),
        ):
            result.update(re.findall(r"[a-z0-9]+", alias.casefold()))
        for alias, _quantity in _special_quantity_aliases(family, "en"):
            result.update(re.findall(r"[a-z0-9]+", alias.casefold()))
        for alias, _canonical in _fixed_aliases(family):
            if alias.isascii():
                result.update(re.findall(r"[a-z0-9]+", alias.casefold()))
    for phrase in (*COMPARISON_MODIFIER_PHRASES, *GROUPING_QUALIFIER_PHRASES):
        result.update(re.findall(r"[a-z0-9]+", phrase.casefold()))
    return result


def analysis_ignored_chinese_phrases() -> set[str]:
    result = {
        "零",
        "一",
        "二",
        "兩",
        "三",
        "四",
        "五",
        "六",
        "七",
        "八",
        "九",
        "十",
        "百",
        "千",
        "萬",
        "億",
        "天",
        "日",
        "週",
        "周",
        "星期",
        "月",
        "個月",
        "年",
        "半年",
        "過去",
        "最近",
        "近",
        "前",
        "本週",
        "這週",
        "上週",
        "本月",
        "這個月",
        "上個月",
        "今年",
        "去年",
        "按日",
        "按天",
        "按週",
        "按周",
        "按月",
        "按年",
        "每日",
        "每天",
        "每週",
        "每周",
        "每月",
        "每年",
        "比較",
        "前期",
        "上一期",
    }
    result.update(_CHINESE_NUMBER_CHARS)
    for family in PHRASE_FAMILIES:
        aliases = _contract_aliases(family)
        result.update(aliases.get("zh_prefixes", ()))
        result.update(aliases.get("zh_units", ()))
        result.update(
            alias for alias, _quantity in _special_quantity_aliases(family, "zh")
        )
        result.update(
            alias for alias, _canonical in _fixed_aliases(family) if not alias.isascii()
        )
    result.update(
        phrase
        for phrase in (*COMPARISON_MODIFIER_PHRASES, *GROUPING_QUALIFIER_PHRASES)
        if not phrase.isascii()
    )
    return result


def period_contract_inventory() -> dict[str, Any]:
    """Return a JSON-safe copy used by capability metadata and eval tests."""

    return _json_safe(PERIOD_PHRASE_CONTRACT)


def period_instruction(policy: Any = None) -> str:
    """Generate shared public wording from the contract and active policy."""

    max_days = _policy_value(
        policy,
        "max_date_range_days",
        DEFAULT_MAX_DATE_RANGE_DAYS,
    )
    time_zone = _policy_value(policy, "time_zone", DEFAULT_TIME_ZONE)
    families = "; ".join(
        f"{_window_kind(family)}: {'；'.join(_phrase_templates(family))}; aliases="
        f"{family.get('aliases', {})}; fixed_aliases={_fixed_aliases(family)}"
        for family in PHRASE_FAMILIES
    )
    return (
        f"Period phrase contract v{PERIOD_PHRASE_CONTRACT_VERSION} resolves the user's "
        f"complete explicit date intent using today in the active policy timezone "
        f"{time_zone!r}. The active QueryPolicy limit is {max_days} "
        "calendar days, calculated as the union of all explicit periods (overlap, "
        "adjacent ranges, and grouping grain do not reduce that requested period). "
        "A valid period at exactly the limit is allowed; limit+1 is rejected before "
        "tenant registry or BigQuery access. Do not split, paginate, shorten, retry, "
        "or switch data tools to work around the limit. The contract vocabulary is: "
        f"{families}. Fixed previous-period comparison is an implicit traffic-summary "
        "report modifier and does not increase requested_days. Invalid or ambiguous "
        "period phrases require clarification; they must never be silently normalized. "
        f"Between two recognized period atoms, only these independent-period joiners are "
        f"allowed: {INDEPENDENT_DATE_PERIOD_JOINERS!r}; any other connective text "
        "requires clarification rather than counting the endpoints separately."
    )


def period_limit_message(policy: Any = None) -> str:
    """Return the concise limitation shown on the OAuth consent page."""

    max_days = _policy_value(
        policy,
        "max_date_range_days",
        DEFAULT_MAX_DATE_RANGE_DAYS,
    )
    time_zone = _policy_value(policy, "time_zone", DEFAULT_TIME_ZONE)
    return (
        f"完整使用者需求的 intent-level 日期上限為 {max_days} 個不重複 calendar days，"
        f"以 {time_zone} 的 today 計算；Phase 10 是 connector best-effort，"
        "單次 tool call 仍由服務端 QueryPolicy 強制限制。"
    )


__all__ = [
    "GROUPING_QUALIFIER_PHRASES",
    "PERIOD_OUTCOMES",
    "PERIOD_PHRASE_CONTRACT",
    "PERIOD_PHRASE_CONTRACT_VERSION",
    "PeriodIntent",
    "PeriodInterval",
    "PeriodPhraseMatch",
    "PeriodSafetyAudit",
    "analysis_ignored_chinese_phrases",
    "analysis_ignored_english_tokens",
    "explicit_date_range_separator_spans",
    "is_period_range_connector",
    "is_query_context_clause",
    "period_contract",
    "period_contract_inventory",
    "period_instruction",
    "period_limit_message",
    "parse_quantity",
    "resolve_period_intent",
    "strip_non_period_filter_values",
]
