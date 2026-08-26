"""Authentication configuration with fail-closed validation."""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlparse


AUTH_MODE_CLOUD_RUN_IAM = "cloud-run-iam"
AUTH_MODE_OAUTH = "oauth"
CLAUDE_CALLBACK_URL = "https://claude.ai/api/mcp/auth_callback"


def _https_url(name: str, value: str, *, allow_localhost: bool = False) -> str:
    parsed = urlparse(value)
    is_localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.netloc or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must be an absolute URL without query or fragment.")
    if parsed.scheme != "https" and not (allow_localhost and is_localhost and parsed.scheme == "http"):
        raise RuntimeError(f"{name} must use HTTPS.")
    return value


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"AUTH_MODE=oauth requires {name}.")
    return value


@dataclass(frozen=True)
class OAuthConfig:
    issuer_url: str
    resource_url: str
    mcp_client_id: str
    mcp_client_secret: str
    google_client_id: str
    google_client_secret: str
    allowed_emails: frozenset[str]
    signing_private_key: str
    signing_key_id: str = "ga4-mcp-poc-1"
    required_scope: str = "ga4:read"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 86400
    authorization_code_ttl_seconds: int = 300
    login_state_ttl_seconds: int = 600

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.issuer_url.rstrip('/')}/oauth/google/callback"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer_url.rstrip('/')}/.well-known/jwks.json"

    @classmethod
    def from_env(cls) -> OAuthConfig:
        issuer_url = _https_url("OAUTH_ISSUER_URL", _required_env("OAUTH_ISSUER_URL"))
        resource_url = _https_url("MCP_PUBLIC_URL", _required_env("MCP_PUBLIC_URL"))
        issuer = urlparse(issuer_url)
        resource = urlparse(resource_url)
        if issuer.path not in {"", "/"}:
            raise RuntimeError("OAUTH_ISSUER_URL must be the service origin without a path.")
        if (issuer.scheme, issuer.netloc) != (resource.scheme, resource.netloc):
            raise RuntimeError("OAUTH_ISSUER_URL and MCP_PUBLIC_URL must use the same origin for this PoC.")
        if not resource_url.endswith("/mcp/"):
            raise RuntimeError("MCP_PUBLIC_URL must end in '/mcp/'.")

        raw_emails = _required_env("OAUTH_ALLOWED_EMAILS")
        allowed_emails = frozenset(email.strip().lower() for email in raw_emails.split(",") if email.strip())
        if not allowed_emails:
            raise RuntimeError("OAUTH_ALLOWED_EMAILS must contain at least one email address.")

        signing_private_key = _required_env("MCP_TOKEN_SIGNING_PRIVATE_KEY").replace("\\n", "\n")
        if "BEGIN PRIVATE KEY" not in signing_private_key and "BEGIN RSA PRIVATE KEY" not in signing_private_key:
            raise RuntimeError("MCP_TOKEN_SIGNING_PRIVATE_KEY must contain a PEM private key.")

        mcp_client_secret = _required_env("MCP_OAUTH_CLIENT_SECRET")
        if len(mcp_client_secret) < 32:
            raise RuntimeError("MCP_OAUTH_CLIENT_SECRET must contain at least 32 characters.")

        signing_key_id = os.getenv("MCP_TOKEN_SIGNING_KEY_ID", "ga4-mcp-poc-1").strip()
        if not signing_key_id:
            raise RuntimeError("MCP_TOKEN_SIGNING_KEY_ID cannot be empty.")

        return cls(
            issuer_url=issuer_url.rstrip("/"),
            resource_url=resource_url,
            mcp_client_id=_required_env("MCP_OAUTH_CLIENT_ID"),
            mcp_client_secret=mcp_client_secret,
            google_client_id=_required_env("GOOGLE_OAUTH_CLIENT_ID"),
            google_client_secret=_required_env("GOOGLE_OAUTH_CLIENT_SECRET"),
            allowed_emails=allowed_emails,
            signing_private_key=signing_private_key,
            signing_key_id=signing_key_id,
        )


def get_auth_mode() -> str:
    mode = os.getenv("AUTH_MODE", AUTH_MODE_CLOUD_RUN_IAM).strip().lower()
    if mode not in {AUTH_MODE_CLOUD_RUN_IAM, AUTH_MODE_OAUTH}:
        raise RuntimeError("AUTH_MODE must be either 'cloud-run-iam' or 'oauth'.")
    return mode
