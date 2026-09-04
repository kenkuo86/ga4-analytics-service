from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery


DEFAULT_MAX_DATE_RANGE_DAYS = 90
DEFAULT_EARLIEST_DATE = date(2020, 10, 14)
DEFAULT_MAX_BYTES_PER_JOB = 2_000_000_000
DEFAULT_MAX_BYTES_PER_REQUEST = 10_000_000_000
DEFAULT_JOB_TIMEOUT_MS = 60_000
DEFAULT_TIME_ZONE = "Asia/Taipei"

_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class QueryPolicyError(ValueError):
    """A GA4 data query was rejected by the shared query policy."""

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


@dataclass(frozen=True)
class PreparedQuery:
    """A parameterized tenant-data query ready for cost preflight."""

    name: str
    sql: str
    query_parameters: Sequence[Any]
    labels: Mapping[str, str]


@dataclass(frozen=True)
class QueryPolicy:
    max_date_range_days: int = DEFAULT_MAX_DATE_RANGE_DAYS
    earliest_date: date = DEFAULT_EARLIEST_DATE
    max_bytes_per_job: int = DEFAULT_MAX_BYTES_PER_JOB
    max_bytes_per_request: int = DEFAULT_MAX_BYTES_PER_REQUEST
    job_timeout_ms: int = DEFAULT_JOB_TIMEOUT_MS
    time_zone: str = DEFAULT_TIME_ZONE

    def __post_init__(self) -> None:
        if self.max_date_range_days <= 0:
            raise ValueError("GA4_QUERY_MAX_DAYS must be greater than zero")
        if self.max_bytes_per_job <= 0:
            raise ValueError("GA4_QUERY_MAX_BYTES_PER_JOB must be greater than zero")
        if self.max_bytes_per_request <= 0:
            raise ValueError(
                "GA4_QUERY_MAX_BYTES_PER_REQUEST must be greater than zero"
            )
        if self.max_bytes_per_request < self.max_bytes_per_job:
            raise ValueError(
                "GA4_QUERY_MAX_BYTES_PER_REQUEST must be greater than or equal to "
                "GA4_QUERY_MAX_BYTES_PER_JOB"
            )
        if self.job_timeout_ms <= 0:
            raise ValueError("GA4_QUERY_JOB_TIMEOUT_MS must be greater than zero")
        try:
            ZoneInfo(self.time_zone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(
                "GA4_QUERY_TIME_ZONE must be a valid IANA time zone"
            ) from error

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> QueryPolicy:
        values = os.environ if environ is None else environ
        earliest_date_text = values.get(
            "GA4_QUERY_EARLIEST_DATE",
            DEFAULT_EARLIEST_DATE.isoformat(),
        )
        if not _ISO_DATE_PATTERN.fullmatch(earliest_date_text):
            raise ValueError("GA4_QUERY_EARLIEST_DATE must use YYYY-MM-DD format")
        try:
            earliest_date = date.fromisoformat(earliest_date_text)
            return cls(
                max_date_range_days=int(
                    values.get(
                        "GA4_QUERY_MAX_DAYS",
                        str(DEFAULT_MAX_DATE_RANGE_DAYS),
                    )
                ),
                earliest_date=earliest_date,
                max_bytes_per_job=int(
                    values.get(
                        "GA4_QUERY_MAX_BYTES_PER_JOB",
                        str(DEFAULT_MAX_BYTES_PER_JOB),
                    )
                ),
                max_bytes_per_request=int(
                    values.get(
                        "GA4_QUERY_MAX_BYTES_PER_REQUEST",
                        str(DEFAULT_MAX_BYTES_PER_REQUEST),
                    )
                ),
                job_timeout_ms=int(
                    values.get(
                        "GA4_QUERY_JOB_TIMEOUT_MS",
                        str(DEFAULT_JOB_TIMEOUT_MS),
                    )
                ),
                time_zone=values.get("GA4_QUERY_TIME_ZONE", DEFAULT_TIME_ZONE),
            )
        except ValueError as error:
            raise ValueError(
                f"Invalid GA4 query policy configuration: {error}"
            ) from error

    def validate_date_range(
        self,
        start_date: str,
        end_date: str,
        *,
        today: date | None = None,
        comparison_periods: int = 0,
    ) -> tuple[date, date]:
        if comparison_periods < 0:
            raise ValueError("comparison_periods must not be negative")
        if (
            not isinstance(start_date, str)
            or not isinstance(end_date, str)
            or not _ISO_DATE_PATTERN.fullmatch(start_date)
            or not _ISO_DATE_PATTERN.fullmatch(end_date)
        ):
            raise QueryPolicyError(
                "invalid_date_format",
                "日期必須使用 YYYY-MM-DD 格式。",
            )
        try:
            parsed_start = date.fromisoformat(start_date)
            parsed_end = date.fromisoformat(end_date)
        except ValueError as error:
            raise QueryPolicyError(
                "invalid_date_format",
                "日期必須是有效的 YYYY-MM-DD 日期。",
            ) from error

        if parsed_start > parsed_end:
            raise QueryPolicyError(
                "invalid_date_range",
                "start_date 不得晚於 end_date。",
            )
        if parsed_start < self.earliest_date:
            raise QueryPolicyError(
                "date_before_available_range",
                f"目前只能查詢 {self.earliest_date.isoformat()} 之後的資料。",
                details={
                    "earliest_date": self.earliest_date.isoformat(),
                    "earliest_scanned_date": parsed_start.isoformat(),
                    "comparison_periods": comparison_periods,
                },
            )

        requested_days = (parsed_end - parsed_start).days + 1
        if requested_days > self.max_date_range_days:
            raise QueryPolicyError(
                "date_range_too_large",
                f"單次 GA4 query 最多查詢 {self.max_date_range_days} 天。",
                details={
                    "requested_days": requested_days,
                    "max_days": self.max_date_range_days,
                },
            )

        earliest_scanned_date = parsed_start - timedelta(
            days=requested_days * comparison_periods
        )
        if earliest_scanned_date < self.earliest_date:
            raise QueryPolicyError(
                "date_before_available_range",
                f"目前只能查詢 {self.earliest_date.isoformat()} 之後的資料。",
                details={
                    "earliest_date": self.earliest_date.isoformat(),
                    "earliest_scanned_date": earliest_scanned_date.isoformat(),
                    "comparison_periods": comparison_periods,
                },
            )

        current_date = today or datetime.now(ZoneInfo(self.time_zone)).date()
        if parsed_end > current_date:
            raise QueryPolicyError(
                "future_date_not_allowed",
                "end_date 不得晚於今天。",
                details={"today": current_date.isoformat()},
            )

        return parsed_start, parsed_end

    def preflight_request(
        self,
        client: bigquery.Client,
        queries: Sequence[PreparedQuery],
    ) -> dict[str, int]:
        """Dry-run every query before execution and enforce job/request limits."""

        estimates: dict[str, int] = {}
        request_bytes = 0
        for query in queries:
            dry_run_config = bigquery.QueryJobConfig(
                query_parameters=list(query.query_parameters),
                dry_run=True,
                use_query_cache=False,
                labels=self._labels(query.labels, stage="cost-preflight"),
            )
            try:
                dry_run_job = client.query(query.sql, job_config=dry_run_config)
                estimated_bytes = int(dry_run_job.total_bytes_processed or 0)
            except Exception as error:
                mapped_error = self.map_bigquery_error(error)
                if mapped_error is not None:
                    raise mapped_error from error
                raise QueryPolicyError(
                    "query_cost_estimate_failed",
                    "目前無法估算查詢成本，因此未執行資料查詢。",
                    details={"query": query.name},
                ) from error

            if estimated_bytes > self.max_bytes_per_job:
                raise QueryPolicyError(
                    "query_cost_limit_exceeded",
                    "預估查詢量超過單一 BigQuery job 上限，因此未執行資料查詢。",
                    details={
                        "query": query.name,
                        "estimated_bytes": estimated_bytes,
                        "max_bytes_per_job": self.max_bytes_per_job,
                    },
                )

            request_bytes += estimated_bytes
            if request_bytes > self.max_bytes_per_request:
                raise QueryPolicyError(
                    "query_cost_limit_exceeded",
                    "同一 tool request 的預估查詢總量超過上限，因此未執行資料查詢。",
                    details={
                        "estimated_request_bytes": request_bytes,
                        "max_bytes_per_request": self.max_bytes_per_request,
                    },
                )
            estimates[query.name] = estimated_bytes
        return estimates

    def execute(
        self,
        client: bigquery.Client,
        query: PreparedQuery,
    ) -> tuple[Any, Any]:
        job_config = bigquery.QueryJobConfig(
            query_parameters=list(query.query_parameters),
            maximum_bytes_billed=self.max_bytes_per_job,
            use_query_cache=True,
            job_timeout_ms=self.job_timeout_ms,
            labels=self._labels(query.labels, stage="execute"),
        )
        try:
            query_job = client.query(query.sql, job_config=job_config)
            rows = query_job.result(timeout=self.job_timeout_ms / 1000)
            return query_job, rows
        except Exception as error:
            mapped_error = self.map_bigquery_error(error)
            if mapped_error is not None:
                raise mapped_error from error
            raise

    def map_bigquery_error(self, error: Exception) -> QueryPolicyError | None:
        errors = getattr(error, "errors", None) or []
        reasons = {
            str(item.get("reason", "")).lower()
            for item in errors
            if isinstance(item, Mapping)
        }
        message = " ".join(
            [
                str(error),
                *(
                    str(item.get("message", ""))
                    for item in errors
                    if isinstance(item, Mapping)
                ),
            ]
        ).lower()

        if "queryusageperday" in message or (
            "usagequotaexceeded" in reasons and "custom quota" in message
        ):
            return QueryPolicyError(
                "daily_query_quota_exceeded",
                "計費專案今日的 BigQuery 查詢額度已用完，請在每日額度重置後再試。",
            )
        if "maximum bytes billed" in message or (
            "bytes billed" in message and "limit" in message
        ):
            return QueryPolicyError(
                "query_cost_limit_exceeded",
                "BigQuery 拒絕了超過單一 job 成本上限的查詢。",
                details={"max_bytes_per_job": self.max_bytes_per_job},
            )
        if isinstance(error, (TimeoutError, google_exceptions.DeadlineExceeded)):
            return QueryPolicyError(
                "query_timeout",
                "BigQuery 查詢超過允許時間，已停止等待結果。",
                details={"job_timeout_ms": self.job_timeout_ms},
            )
        return None

    @staticmethod
    def _labels(labels: Mapping[str, str], *, stage: str) -> dict[str, str]:
        return {
            **labels,
            "cost-policy": "v1",
            "stage": stage,
        }


query_policy = QueryPolicy.from_environment()
