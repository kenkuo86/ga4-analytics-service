"""OAuth resource-server support shared by the REST and MCP endpoints.

Cloud Run IAM remains the authentication boundary while AUTH_MODE is left at
its default.  Before making the Cloud Run transport publicly reachable, deploy
with AUTH_MODE=oauth and the issuer/JWKS settings below so both data endpoints
fail closed unless they receive a valid, audience-bound access token.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url
from pydantic import AnyHttpUrl


LOGGER = logging.getLogger(__name__)


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


@dataclass(frozen=True)
class OAuthConfig:
    issuer_url: str
    jwks_url: str
    resource_url: str
    audience: str
    required_scopes: tuple[str, ...]
    allowed_algorithms: tuple[str, ...]

    @classmethod
    def from_env(cls) -> OAuthConfig | None:
        auth_mode = os.getenv("AUTH_MODE", "cloud-run-iam").strip().lower()
        if auth_mode == "cloud-run-iam":
            return None
        if auth_mode != "oauth":
            raise RuntimeError("AUTH_MODE must be either 'cloud-run-iam' or 'oauth'.")

        required = {
            "OAUTH_ISSUER_URL": os.getenv("OAUTH_ISSUER_URL", "").strip(),
            "OAUTH_JWKS_URL": os.getenv("OAUTH_JWKS_URL", "").strip(),
            "MCP_PUBLIC_URL": os.getenv("MCP_PUBLIC_URL", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"AUTH_MODE=oauth requires: {', '.join(missing)}")

        for name, value in required.items():
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise RuntimeError(f"{name} must be an absolute HTTPS URL.")

        resource_url = required["MCP_PUBLIC_URL"]
        if not resource_url.endswith("/mcp/"):
            raise RuntimeError("MCP_PUBLIC_URL must be the public connector URL ending in '/mcp/'.")

        audience = os.getenv("OAUTH_AUDIENCE", resource_url).strip()
        if not audience:
            raise RuntimeError("OAUTH_AUDIENCE cannot be empty.")

        required_scopes = _csv_env("OAUTH_REQUIRED_SCOPES", "ga4:read")
        if not required_scopes:
            raise RuntimeError("OAUTH_REQUIRED_SCOPES must contain at least one scope.")

        allowed_algorithms = _csv_env("OAUTH_ALLOWED_ALGORITHMS", "RS256")
        if not allowed_algorithms:
            raise RuntimeError("OAUTH_ALLOWED_ALGORITHMS must contain at least one algorithm.")

        return cls(
            issuer_url=required["OAUTH_ISSUER_URL"],
            jwks_url=required["OAUTH_JWKS_URL"],
            resource_url=resource_url,
            audience=audience,
            required_scopes=required_scopes,
            allowed_algorithms=allowed_algorithms,
        )

    @property
    def resource_metadata_url(self) -> str:
        return str(build_resource_metadata_url(AnyHttpUrl(self.resource_url)))


class JWTTokenVerifier(TokenVerifier):
    """Validate signed OAuth access tokens using the issuer's public JWKS."""

    def __init__(self, config: OAuthConfig):
        self.config = config
        self._jwks_client = PyJWKClient(config.jwks_url, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        # PyJWKClient performs a blocking HTTPS fetch on the first key lookup.
        # Keep that work off the ASGI event loop; keys are cached afterwards.
        return await asyncio.to_thread(self._verify_token_sync, token)

    def _verify_token_sync(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.config.allowed_algorithms),
                audience=self.config.audience,
                issuer=self.config.issuer_url,
                leeway=30,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            LOGGER.info("Rejected OAuth access token: %s", type(exc).__name__)
            return None
        except Exception:
            # Do not log the token or provider response. A transient JWKS failure
            # must fail closed just like an invalid token.
            LOGGER.exception("Unable to validate OAuth access token")
            return None

        raw_scopes = claims.get("scope", claims.get("scp", []))
        if isinstance(raw_scopes, str):
            scopes = raw_scopes.split()
        elif isinstance(raw_scopes, list) and all(isinstance(scope, str) for scope in raw_scopes):
            scopes = raw_scopes
        else:
            return None

        subject = claims["sub"]
        client_id = claims.get("client_id") or claims.get("azp") or subject

        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.config.audience,
            subject=str(subject),
            claims=claims,
        )


oauth_config = OAuthConfig.from_env()
oauth_token_verifier = JWTTokenVerifier(oauth_config) if oauth_config else None


async def require_rest_oauth(request: Request) -> AccessToken | None:
    """Apply the same bearer-token policy to the preserved REST endpoint."""

    if oauth_config is None or oauth_token_verifier is None:
        # In this mode the private Cloud Run IAM check is the outer boundary.
        return None

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _auth_error(401, "Authentication required")

    access_token = await oauth_token_verifier.verify_token(token)
    if access_token is None:
        raise _auth_error(401, "Invalid or expired access token")

    missing_scopes = set(oauth_config.required_scopes) - set(access_token.scopes)
    if missing_scopes:
        raise _auth_error(403, "Insufficient scope")

    return access_token


def _auth_error(status_code: int, detail: str) -> HTTPException:
    assert oauth_config is not None
    challenge = (
        f'Bearer resource_metadata="{oauth_config.resource_metadata_url}", '
        f'scope="{" ".join(oauth_config.required_scopes)}"'
    )
    return HTTPException(status_code=status_code, detail=detail, headers={"WWW-Authenticate": challenge})
