"""Shared tenant request context for public tool results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass
class TenantRequestContext:
    """Track tenant resolution state throughout one public tool request."""

    requested_name: str
    resolved_name: str | None = None
    match_type: str = "none"

    @classmethod
    def from_customer_name(cls, customer_name: Any) -> TenantRequestContext:
        requested_name = (
            customer_name.strip()
            if isinstance(customer_name, str)
            else str(customer_name)
        )
        return cls(requested_name=requested_name)

    def resolve_from_tenant(self, tenant: Mapping[str, Any]) -> None:
        """Advance the request context after tenant routing succeeds."""

        requested_name = tenant.get("requested_name")
        if requested_name is not None:
            self.requested_name = str(requested_name)
        resolved_name = tenant.get("resolved_name")
        self.resolved_name = (
            str(resolved_name) if resolved_name is not None else None
        )
        self.match_type = str(tenant.get("match_type") or "none")


class TenantContextErrorMixin:
    """Provide one tenant-context contract for public domain errors."""

    requested_name: str | None
    resolved_name: str | None
    match_type: str | None

    def _init_tenant_context(
        self,
        *,
        requested_name: str | None = None,
        resolved_name: str | None = None,
        match_type: str | None = None,
    ) -> None:
        self.requested_name = requested_name
        self.resolved_name = resolved_name
        self.match_type = match_type

    def attach_tenant_context(
        self,
        *,
        requested_name: str,
        resolved_name: str | None,
        match_type: str,
    ) -> None:
        self.requested_name = requested_name
        self.resolved_name = resolved_name
        self.match_type = match_type

    def attach_request_context(self, context: TenantRequestContext) -> None:
        """Fill missing context without replacing a more specific error state."""

        if self.requested_name is None:
            self.requested_name = context.requested_name
        if self.resolved_name is None and context.resolved_name is not None:
            self.resolved_name = context.resolved_name
            self.match_type = context.match_type
        elif self.match_type is None:
            self.match_type = context.match_type

    def tenant_context_result(self) -> dict[str, str | None]:
        if self.requested_name is None:
            return {}
        return {
            "requested_name": self.requested_name,
            "resolved_name": self.resolved_name,
            "match_type": self.match_type or "none",
        }
