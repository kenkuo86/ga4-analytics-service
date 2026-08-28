import json

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from main import app as api_app
from main import (
    TenantResolutionError,
    get_available_customers,
    get_customer_status,
    get_traffic_summary,
    query_ga4_semantic_metrics,
    search_ga4_metric_catalog,
)
from oauth_auth import oauth_runtime
from oauth_server import jwks_response
from semantic_catalog import SemanticCatalogError

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

mcp = MCPServer(
    "GA4 Analytics Service",
    instructions="""
This server resolves every customer name through the tenant registry. Users
never need to know or provide tenant_id, project_id, or dataset_id. Use the
customer name from the conversation in each tool call; tool results include
data_source routing metadata when it is configured. Treat tenant_id,
project_id, and dataset_id as internal metadata and do not show them in the
answer unless the user explicitly asks for technical routing details.

When the user asks which customers are available, call
list_available_customers and present its customer names. Do not replace the
customer list with a registry spreadsheet link.

For analytics beyond traffic_summary, first use search_ga4_metrics to find the
published metric IDs in the versioned semantic catalog, then call query_ga4
with only those IDs. Catalog metrics may include source, medium, campaign,
content, conversion, and ecommerce analyses. Never invent a metric ID or SQL.
query_ga4 resolves ecommerce versus non-ecommerce from the tenant registry ec
field; never ask the user to identify the site type. Never ask the user for a
project ID or dataset ID to work around a missing capability.

Each semantic metric result includes date_scope. If it is all_available_data,
state that the metric definition is an all-data snapshot and do not describe it
as limited to the requested period.

This server does not provide ads, SEO keyword ranking, CRM, or arbitrary
BigQuery access. Never present general knowledge or an inference as actual
customer data, and never claim a catalog metric was queried unless a tool
returned status ok.
""".strip(),
    **mcp_auth_kwargs,
)


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
def customer_lookup(customer_name: str) -> dict:
    """
    Check whether an exact customer name exists in the tenant registry.

    Use this tool whenever the user asks whether a customer exists. This lookup
    does not require access to the customer's GA4 dataset. A customer can exist
    even when analytics_available is false. If the result is tenant_not_found,
    do not claim that a similar customer exists and do not guess another name.
    When configured, data_source contains the project_id and dataset_id for
    internal routing. Never ask the user to provide either identifier.
    """
    try:
        return get_customer_status(customer_name)
    except TenantResolutionError as error:
        return error.as_result()


@mcp.tool()
def list_available_customers() -> dict:
    """
    List customer names currently available for GA4 traffic summary queries.

    Use this tool when the user asks which customers or accounts can be
    queried. It returns only registry entries that are active, have a
    configured project, have a non-empty customer name, and can be uniquely
    resolved by that name. Present the customer names directly. Do not expose
    tenant IDs or project IDs, and do not ask the user to inspect the registry
    spreadsheet instead.
    """
    try:
        return get_available_customers()
    except Exception:
        return {
            "status": "data_unavailable",
            "message": "目前無法取得可查詢的客戶清單，請稍後再試。",
        }


@mcp.tool()
def search_ga4_metrics(
    query: str,
    profile: str | None = None,
    limit: int = 10,
) -> dict:
    """
    Search the versioned GA4 semantic catalog for supported metric IDs.

    Use this before query_ga4 whenever the user asks for an analysis beyond the
    fixed traffic summary or uses a natural-language metric name. The search
    covers metric labels, report context, and dimensions such as source,
    medium, campaign, date, device, geography, page, and item. Pass profile as
    ecommerce or non_ecommerce only when the website type is already known.
    This tool reads definitions only; it does not query customer data.
    """
    try:
        return search_ga4_metric_catalog(
            query=query,
            profile=profile,
            limit=limit,
        )
    except SemanticCatalogError as error:
        return error.as_result()


@mcp.tool()
def query_ga4(
    customer_name: str,
    metric_ids: list[str],
    start_date: str,
    end_date: str,
    limit: int = 50,
) -> dict:
    """
    Query one to five published GA4 semantic metrics for a customer and period.

    metric_ids must come from search_ga4_metrics; never invent IDs. The server
    resolves project_id, dataset_id, and the ecommerce profile from the tenant
    registry, compiles only catalog-approved SQL, and never accepts raw table,
    column, filter, group by, profile, or SQL input. Present only rows returned
    with status ok and retain routing metadata as internal context. Respect
    each metric's date_scope: do not describe an all_available_data result as
    limited to start_date and end_date.
    """
    try:
        return query_ga4_semantic_metrics(
            customer_name=customer_name,
            metric_ids=metric_ids,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except (TenantResolutionError, SemanticCatalogError) as error:
        return error.as_result()


@mcp.tool()
def traffic_summary(
    customer_name: str,
    start_date: str,
    end_date: str,
) -> dict:
    """
    Get GA4 traffic summary by the customer's registered name and date range.

    Returns current period, previous period, and percentage change for:
    total sessions, total users, new users, and returning users.

    Always use the customer name stated by the user. If the result status is
    tenant_not_found, tell the user that the customer does not exist in the
    tenant registry. If it is tenant_inactive, explain that the customer exists
    but is not currently available. Never guess a different customer. The
    result includes data_source routing metadata; retain it as context and
    never ask the user for project_id or dataset_id. For supported follow-up
    analyses such as source, medium, or campaign, use search_ga4_metrics and
    query_ga4 rather than claiming arbitrary BigQuery access.
    """
    try:
        return get_traffic_summary(
            customer_name=customer_name,
            start_date=start_date,
            end_date=end_date,
        )
    except TenantResolutionError as error:
        return error.as_result()


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
