import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server import MCPServer

from main import app as api_app
from main import get_traffic_summary
from oauth_auth import oauth_config, oauth_token_verifier

from mcp.server.transport_security import TransportSecuritySettings

mcp_auth_kwargs = {}
if oauth_config is not None:
    mcp_auth_kwargs = {
        "token_verifier": oauth_token_verifier,
        "auth": AuthSettings(
            # Pass strings so AuthSettings can preserve the issuer's exact
            # trailing-slash semantics for RFC 8414 issuer comparison.
            issuer_url=oauth_config.issuer_url,
            resource_server_url=oauth_config.resource_url,
            required_scopes=list(oauth_config.required_scopes),
        ),
    }

mcp = MCPServer("GA4 Analytics Service", **mcp_auth_kwargs)


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
    streamable_http_path="/mcp/",
    transport_security=security,
)

# Keep the MCP app at the ASGI root.  This lets MCP 2.1.0 expose RFC 9728
# metadata at /.well-known/oauth-protected-resource/mcp/ rather than nesting
# that route incorrectly below /mcp/.  The existing FastAPI app is the final
# fallback route and therefore preserves /traffic-summary.
mcp_app.mount("/", api_app)
app = mcp_app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
    )
