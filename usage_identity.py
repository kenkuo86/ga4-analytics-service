"""Request-local trusted identity; never retain raw subjects or bearer tokens."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import json
import os
import time
from uuid import uuid4

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from query_policy import QueryPolicyError
from usage_contract import tenant_id_for_event

DEPARTMENT_SCOPE = "department-active-tenants-v1"
IAM_SCOPE = "cloud-run-iam-active-tenants-v1"


@dataclass(frozen=True)
class IdentitySettings:
    key: bytes | None = field(default=None, repr=False)
    client_hosts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls):
        raw = os.getenv("USAGE_IDENTITY_KEY", "")
        try:
            key = base64.b64decode(raw, validate=True) if raw else None
        except Exception:
            raise RuntimeError("USAGE_IDENTITY_KEY must be base64 encoded") from None
        if key is not None and len(key) < 32:
            raise RuntimeError("USAGE_IDENTITY_KEY requires at least 32 random bytes")
        if os.getenv("USAGE_ENABLED", "false").lower() == "true" and key is None:
            raise RuntimeError("USAGE_ENABLED requires a persistent USAGE_IDENTITY_KEY")
        try:
            hosts = json.loads(os.getenv("USAGE_CLIENT_HOSTS", "{}"))
            if not isinstance(hosts, dict) or len(hosts) > 100:
                raise ValueError()
            for client, host in hosts.items():
                if not isinstance(client, str) or not 1 <= len(client) <= 256 or host not in (
                    "claude", "chatgpt", "web_app", "other",
                ):
                    raise ValueError()
        except Exception:
            raise RuntimeError("USAGE_CLIENT_HOSTS requires a bounded client-to-host enum mapping") from None
        return cls(key=key, client_hosts=hosts)


settings = IdentitySettings.from_env()


@dataclass
class AnalyticsContext:
    transport: str
    interaction_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: float = field(default_factory=time.monotonic)
    user_id: str | None = None
    host: str = "other"
    authenticated: bool = False
    authorized: bool = False
    authorization_scope_ref: str | None = None
    authorization_result: str = "unknown"
    tenant_id: str | None = None
    tenant_id_quality: str | None = None


current_context: ContextVar[AnalyticsContext | None] = ContextVar("analytics_context", default=None)


@contextmanager
def bind_context(context: AnalyticsContext):
    reset = current_context.set(context)
    try:
        yield context
    finally:
        current_context.reset(reset)


def trusted_context(
    access: AccessToken | None, *, mode: str, transport: str,
    required_scope: str = "ga4:read", identity_settings: IdentitySettings | None = None,
) -> AnalyticsContext:
    """Caller MUST pass an already verified token, never parsed client claims."""
    config = identity_settings if identity_settings is not None else settings
    context = AnalyticsContext(transport=transport)
    if mode == "cloud-run-iam":
        # External Cloud Run IAM remains the security boundary, not an app header.
        context.authorized = True
        context.authorization_scope_ref = IAM_SCOPE
        return context
    subject = access.subject if access is not None else None
    if mode != "oauth" or not isinstance(subject, str) or not subject:
        context.authorization_result = "denied"
        return context
    context.authenticated = True
    context.authorized = required_scope in access.scopes
    context.authorization_scope_ref = DEPARTMENT_SCOPE
    if not context.authorized:
        context.authorization_result = "denied"
    context.host = config.client_hosts.get(access.client_id, "other")
    # Telemetry failure must never revoke otherwise valid analytics authorization.
    try:
        if config.key is not None:
            message = json.dumps(["https://accounts.google.com", subject], separators=(",", ":"))
            context.user_id = hmac.new(config.key, message.encode(), hashlib.sha256).hexdigest()
    except Exception:
        context.user_id = None
    return context


def require_tenant_access() -> None:
    """Guard data/registry access in public contexts, independent of telemetry flags."""
    context = current_context.get()
    # Local CLI and offline validation do not enter a public HTTP request context.
    if context is not None and not context.authorized:
        context.authorization_result = "denied"
        raise QueryPolicyError("tenant_access_denied", "目前身分沒有客戶資料存取權限。")


def record_resolved_tenant(tenant_id: object, *, analytics_allowed: bool = True) -> None:
    context = current_context.get()
    if context is None:
        return
    require_tenant_access()
    context.authorization_result = "allowed" if analytics_allowed else "denied"
    try:
        context.tenant_id, context.tenant_id_quality = tenant_id_for_event(tenant_id)
    except Exception:
        context.tenant_id = None
        context.tenant_id_quality = "tenant_id_type_invalid"


class MCPIdentityMiddleware:
    def __init__(self, runtime):
        self.runtime = runtime

    async def __call__(self, ctx, call_next):
        # Use THIS message's verified HTTP user, not connection/session ContextVars.
        user = ctx.request.scope.get("user") if ctx.request is not None else None
        access = user.access_token if isinstance(user, AuthenticatedUser) else None
        required = self.runtime.config.required_scope if self.runtime.config else "ga4:read"
        context = trusted_context(access, mode=self.runtime.mode, transport="mcp", required_scope=required)
        with bind_context(context):
            return await call_next(ctx)
