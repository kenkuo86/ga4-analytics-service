"""Runtime authentication wiring shared by REST and MCP."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url
from pydantic import AnyHttpUrl

from auth_config import AUTH_MODE_CLOUD_RUN_IAM, AUTH_MODE_OAUTH, OAuthConfig, get_auth_mode
from oauth_server import GoogleOAuthAuthorizationServer
from usage_identity import bind_context, trusted_context


@dataclass
class AuthRuntime:
    mode: str
    config: OAuthConfig | None = None
    provider: GoogleOAuthAuthorizationServer | None = None

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.provider is None:
            return None
        return await self.provider.load_access_token(token)


def load_auth_runtime() -> AuthRuntime:
    mode = get_auth_mode()
    if mode == AUTH_MODE_CLOUD_RUN_IAM:
        return AuthRuntime(mode=mode)
    if mode == AUTH_MODE_OAUTH:
        config = OAuthConfig.from_env()
        return AuthRuntime(mode=mode, config=config, provider=GoogleOAuthAuthorizationServer(config))
    raise AssertionError("unreachable")


oauth_runtime = load_auth_runtime()


async def require_rest_oauth(request: Request):
    """Keep verified identity alive through the entire REST dependency lifetime."""
    access = await _verify_rest_oauth(request)
    runtime = oauth_runtime
    context = trusted_context(
        access, mode=runtime.mode, transport="rest",
        required_scope=runtime.config.required_scope if runtime.config else "ga4:read",
    )
    transport = request.scope.get('usage_transport')
    if transport is not None:
        context.started_at = transport['started_at']
        transport['context'] = context
    from usage_logging import begin, emitter
    if emitter.enabled:
        try:
            # REST carries only enum hints; no free-text summary in GET URL/access logs.
            begin(context, 'traffic_summary', {
                'analysis_goal_hint': request.query_params.get('analysis_goal_hint'),
                'analysis_subject_hint': request.query_params.get('analysis_subject_hint'),
            })
        except Exception:
            pass
    with bind_context(context):
        yield access


async def _verify_rest_oauth(request: Request) -> AccessToken | None:
    """Apply the same bearer-token policy to the preserved REST endpoint."""

    runtime = oauth_runtime
    if runtime.mode == AUTH_MODE_CLOUD_RUN_IAM:
        return None
    assert runtime.config is not None

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _auth_error(runtime.config, 401, "Authentication required")

    access_token = await runtime.verify_token(token)
    if access_token is None:
        raise _auth_error(runtime.config, 401, "Invalid or expired access token")
    if runtime.config.required_scope not in access_token.scopes:
        raise _auth_error(runtime.config, 403, "Insufficient scope")
    return access_token


def _auth_error(config: OAuthConfig, status_code: int, detail: str) -> HTTPException:
    metadata_url = build_resource_metadata_url(AnyHttpUrl(config.resource_url))
    challenge = (
        f'Bearer resource_metadata="{metadata_url}", '
        f'scope="{config.required_scope}"'
    )
    return HTTPException(status_code=status_code, detail=detail, headers={"WWW-Authenticate": challenge})
