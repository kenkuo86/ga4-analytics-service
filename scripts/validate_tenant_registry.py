"""Validate tenant names and managed aliases before a registry rollout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from google.cloud import bigquery  # noqa: E402

from main import REGISTRY_TABLE  # noqa: E402
from scripts.validate_all_tenants import (  # noqa: E402
    DEFAULT_BILLING_PROJECT,
    get_credentials,
)
from tenant_registry import (  # noqa: E402
    TenantRegistryValidationError,
    validate_registry_rows,
)


def get_registry_rows(
    client: bigquery.Client,
    registry_table: str,
) -> list[Any]:
    """Read every registry row once, including inactive rows and aliases."""

    sql = f"""
    SELECT
      tenant_id,
      tenant_name,
      aliases
    FROM `{registry_table}`
    ORDER BY tenant_id
    """
    return list(client.query(sql).result())


def validate_registry(
    client: bigquery.Client,
    registry_table: str,
) -> dict[str, Any]:
    rows = get_registry_rows(client, registry_table)
    report = validate_registry_rows(rows)
    return {
        **report,
        "registry_table": registry_table,
        "registry_query_count": 1,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate tenant formal names and managed aliases without querying GA4 data."
        )
    )
    parser.add_argument(
        "--billing-project",
        default=os.getenv("BIGQUERY_BILLING_PROJECT", DEFAULT_BILLING_PROJECT),
    )
    parser.add_argument(
        "--impersonate-service-account",
        default=None,
        help="Optional runtime service account used to read the registry.",
    )
    parser.add_argument(
        "--use-gcloud-source-credentials",
        action="store_true",
        help="Use the active gcloud account as the impersonation source.",
    )
    parser.add_argument(
        "--registry-table",
        default=os.getenv("TENANT_REGISTRY_TABLE", REGISTRY_TABLE),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON output path. stdout is always written.",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    credentials = get_credentials(
        args.impersonate_service_account,
        use_gcloud_source_credentials=args.use_gcloud_source_credentials,
    )
    client = bigquery.Client(
        credentials=credentials,
        project=args.billing_project,
    )
    try:
        report = validate_registry(client, args.registry_table)
        exit_code = 0
    except TenantRegistryValidationError as error:
        report = {
            **error.as_result(),
            "registry_table": args.registry_table,
            "registry_query_count": 1,
        }
        exit_code = 1

    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
