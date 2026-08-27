import json

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from main import app as api_app
from main import get_traffic_summary
from oauth_auth import oauth_runtime
from oauth_server import jwks_response

from mcp.server.transport_security import TransportSecuritySettings

mcp_auth_kwargs = {}
if oauth_runtime.config is not None and oauth_runtime.provider is not None:
    oauth_config = oauth_runtime.config
    mcp_auth_kwargs = {
        "auth_server_provider": oauth_runtime.provider,
        "auth": AuthSettings(
            issuer_url=oauth_config.issuer_url,
            resource_server_url=oauth_config.resource_url,
            required_scopes=[oauth_config.required_scope],
            client_registration_options=ClientRegistrationOptions(
                enabled=False,
                valid_scopes=[oauth_config.required_scope],
                default_scopes=[oauth_config.required_scope],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    }

mcp = MCPServer("GA4 Analytics Service", **mcp_auth_kwargs)


if oauth_runtime.provider is not None:
    oauth_provider = oauth_runtime.provider

    @mcp.custom_route("/oauth/google/callback", methods=["GET"])
    async def google_oauth_callback(request: Request):
        return await oauth_provider.google_callback(request)

    @mcp.custom_route("/oauth/consent", methods=["POST"])
    async def oauth_consent(request: Request):
        return await oauth_provider.consent(request)

    @mcp.custom_route("/.well-known/jwks.json", methods=["GET"])
    async def oauth_jwks(request: Request):
        return jwks_response(oauth_provider)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok"})


@mcp.tool()
def traffic_summary(
    tenant_id: str,
    start_date: str,
    end_date: str,
) -> dict:
    """
    Get GA4 traffic summary for a tenant and date range.

    Returns current period, previous period, and percentage change for:
    total sessions, total users, new users, and returning users.
    """
    return get_traffic_summary(
        tenant_id=tenant_id,
        start_date=start_date,
        end_date=end_date,
    )


security = TransportSecuritySettings(
    allowed_hosts=[
        "ga4-analytics-service-398991472921.asia-east1.run.app",
    ],
)

mcp_app = mcp.streamable_http_app(
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=security,
)

# Keep the MCP app at the ASGI root. This lets MCP 2.1.0 expose RFC 9728
# metadata at /.well-known/oauth-protected-resource/mcp rather than nesting
# that route below /mcp. The existing FastAPI app remains the final fallback.
mcp_app.mount("/", api_app)


class OAuthPublicClientMetadataMiddleware:
    """Correct MCP 2.1.0 metadata for the pre-registered public client."""

    metadata_path = "/.well-known/oauth-authorization-server"

    def __init__(self, wrapped_app: ASGIApp):
        self.wrapped_app = wrapped_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self.metadata_path:
            await self.wrapped_app(scope, receive, send)
            return

        response_start = None
        response_body = bytearray()

        async def capture(message):
            nonlocal response_start
            if message["type"] == "http.response.start":
                response_start = message
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    metadata = json.loads(response_body)
                    metadata["token_endpoint_auth_methods_supported"] = ["none"]
                    if "revocation_endpoint_auth_methods_supported" in metadata:
                        metadata["revocation_endpoint_auth_methods_supported"] = ["none"]
                    body = json.dumps(metadata, separators=(",", ":")).encode()
                    assert response_start is not None
                    headers = [
                        (name, value)
                        for name, value in response_start["headers"]
                        if name.lower() != b"content-length"
                    ]
                    headers.append((b"content-length", str(len(body)).encode()))
                    await send({**response_start, "headers": headers})
                    await send({"type": "http.response.body", "body": body})

        await self.wrapped_app(scope, receive, capture)


class MCPPathCompatibilityMiddleware:
    """Accept Claude's /mcp normalization without breaking existing /mcp/ clients."""

    aliases = {
        "/mcp/": "/mcp",
        "/.well-known/oauth-protected-resource/mcp/": "/.well-known/oauth-protected-resource/mcp",
    }

    def __init__(self, wrapped_app: ASGIApp):
        self.wrapped_app = wrapped_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in self.aliases:
            scope = dict(scope)
            scope["path"] = self.aliases[scope["path"]]
            scope["raw_path"] = scope["path"].encode("ascii")
        await self.wrapped_app(scope, receive, send)


app = MCPPathCompatibilityMiddleware(OAuthPublicClientMetadataMiddleware(mcp_app))


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
    )
