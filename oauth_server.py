"""Small, single-instance OAuth authorization server for the Claude Web PoC.

Google OIDC authenticates the human. This provider then issues its own
audience-bound MCP tokens; Google access tokens are never accepted by MCP.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from html import escape
import re
import secrets
import time
from typing import Any, Protocol
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from auth_config import CLAUDE_CALLBACK_URL, OAuthConfig


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


class GoogleIdentityProvider(Protocol):
    async def exchange_code(self, code: str, expected_nonce: str) -> dict[str, Any]: ...


class GoogleOIDCClient:
    def __init__(self, config: OAuthConfig):
        self.config = config

    async def exchange_code(self, code: str, expected_nonce: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._exchange_code_sync, code, expected_nonce)

    def _exchange_code_sync(self, code: str, expected_nonce: str) -> dict[str, Any]:
        response = requests.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": self.config.google_client_id,
                "client_secret": self.config.google_client_secret,
                "redirect_uri": self.config.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        if response.status_code != 200:
            raise ValueError("Google token exchange failed")

        token_response = response.json()
        raw_id_token = token_response.get("id_token")
        if not isinstance(raw_id_token, str):
            raise ValueError("Google token response did not include an ID token")

        claims = google_id_token.verify_oauth2_token(
            raw_id_token,
            GoogleAuthRequest(),
            self.config.google_client_id,
            clock_skew_in_seconds=30,
        )
        if claims.get("nonce") != expected_nonce:
            raise ValueError("Google ID token nonce mismatch")
        return claims


@dataclass(frozen=True)
class PendingAuthorization:
    client_id: str
    client_state: str | None
    scopes: tuple[str, ...]
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str
    google_nonce: str
    expires_at: float


@dataclass(frozen=True)
class PendingConsent:
    authorization: PendingAuthorization
    subject: str
    email: str
    expires_at: float


class MCPRefreshToken(RefreshToken):
    resource: str


def _b64url_uint(value: int) -> str:
    size = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode("ascii")


class GoogleOAuthAuthorizationServer(
    OAuthAuthorizationServerProvider[AuthorizationCode, MCPRefreshToken, AccessToken]
):
    """Public, pre-registered Claude client plus Google user login."""

    def __init__(self, config: OAuthConfig, google_identity: GoogleIdentityProvider | None = None):
        self.config = config
        self.google_identity = google_identity or GoogleOIDCClient(config)
        self._private_key = serialization.load_pem_private_key(config.signing_private_key.encode(), password=None)
        if not isinstance(self._private_key, rsa.RSAPrivateKey):
            raise RuntimeError("MCP token signing key must be an RSA private key.")
        if self._private_key.key_size < 2048:
            raise RuntimeError("MCP token signing key must be at least 2048 bits.")
        self._public_key = self._private_key.public_key()

        self._client = OAuthClientInformationFull(
            client_id=config.mcp_client_id,
            client_name="Claude Web GA4 Connector",
            redirect_uris=[AnyUrl(CLAUDE_CALLBACK_URL)],
            response_types=["code"],
            grant_types=["authorization_code", "refresh_token"],
            scope=config.required_scope,
            token_endpoint_auth_method="none",
        )
        self._pending_logins: dict[str, PendingAuthorization] = {}
        self._pending_consents: dict[str, PendingConsent] = {}
        self._authorization_codes: dict[str, AuthorizationCode] = {}
        self._refresh_tokens: dict[str, MCPRefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._client if secrets.compare_digest(client_id, self.config.mcp_client_id) else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("Dynamic client registration is disabled for this PoC.")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._cleanup()
        # Claude Web can omit the RFC 8707 resource parameter when it falls
        # back to authorization-server discovery. This server has exactly one
        # protected resource, so safely default an omitted value to it. Accept
        # the historical trailing-slash spelling as the same local resource.
        requested_resource = str(params.resource).rstrip("/") if params.resource else self.config.resource_url
        if requested_resource != self.config.resource_url:
            raise AuthorizeError("invalid_target", "The requested resource is not this MCP server.")

        scopes = tuple(params.scopes or [self.config.required_scope])
        if set(scopes) != {self.config.required_scope}:
            raise AuthorizeError("invalid_scope", "Only the ga4:read scope is supported.")
        if not client.client_id:
            raise AuthorizeError("invalid_request", "Missing client ID.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "PKCE S256 code challenge is invalid.")

        login_state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        self._pending_logins[login_state] = PendingAuthorization(
            client_id=client.client_id,
            client_state=params.state,
            scopes=scopes,
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.config.resource_url,
            google_nonce=nonce,
            expires_at=time.time() + self.config.login_state_ttl_seconds,
        )

        query = urlencode(
            {
                "client_id": self.config.google_client_id,
                "redirect_uri": self.config.google_redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": login_state,
                "nonce": nonce,
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"

    async def google_callback(self, request: Request) -> Response:
        self._cleanup()
        state = request.query_params.get("state", "")
        pending = self._pending_logins.pop(state, None)
        if pending is None:
            raise HTTPException(400, "Invalid or expired Google OAuth state.")

        if request.query_params.get("error"):
            return self._client_error_redirect(pending, "access_denied", "Google sign-in was not completed.")

        code = request.query_params.get("code")
        if not code:
            return self._client_error_redirect(pending, "server_error", "Google did not return an authorization code.")

        try:
            claims = await self.google_identity.exchange_code(code, pending.google_nonce)
        except Exception:
            return self._client_error_redirect(pending, "server_error", "Google identity verification failed.")

        email = str(claims.get("email", "")).lower()
        email_verified = claims.get("email_verified") in {True, "true"}
        subject = claims.get("sub")
        if not email_verified or not subject or email not in self.config.allowed_emails:
            return self._client_error_redirect(pending, "access_denied", "This Google account is not authorized.")

        consent_token = secrets.token_urlsafe(32)
        self._pending_consents[consent_token] = PendingConsent(
            authorization=pending,
            subject=str(subject),
            email=email,
            expires_at=time.time() + self.config.login_state_ttl_seconds,
        )
        return self._consent_page(consent_token, email)

    async def consent(self, request: Request) -> Response:
        self._cleanup()
        form = await request.form()
        consent_token = form.get("consent_token")
        decision = form.get("decision")
        if not isinstance(consent_token, str):
            raise HTTPException(400, "Missing consent token.")

        pending = self._pending_consents.pop(consent_token, None)
        if pending is None:
            raise HTTPException(400, "Invalid or expired consent token.")
        if decision != "approve":
            return self._client_error_redirect(pending.authorization, "access_denied", "Access was denied.")

        authorization_code = f"mcp_code_{secrets.token_urlsafe(32)}"
        auth = pending.authorization
        self._authorization_codes[authorization_code] = AuthorizationCode(
            code=authorization_code,
            scopes=list(auth.scopes),
            expires_at=time.time() + self.config.authorization_code_ttl_seconds,
            client_id=auth.client_id,
            code_challenge=auth.code_challenge,
            redirect_uri=AnyUrl(auth.redirect_uri),
            redirect_uri_provided_explicitly=auth.redirect_uri_provided_explicitly,
            resource=auth.resource,
            subject=pending.subject,
        )
        return RedirectResponse(
            construct_redirect_uri(auth.redirect_uri, code=authorization_code, state=auth.client_state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._cleanup()
        code = self._authorization_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = self._authorization_codes.pop(authorization_code.code, None)
        if stored is None or stored.client_id != client.client_id or stored.resource != self.config.resource_url:
            raise TokenError("invalid_grant", "Authorization code is invalid or already used.")
        if not stored.subject:
            raise TokenError("invalid_grant", "Authorization code has no resource owner.")
        return self._issue_token_pair(
            client_id=stored.client_id,
            subject=stored.subject,
            scopes=stored.scopes,
            resource=stored.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> MCPRefreshToken | None:
        self._cleanup()
        stored = self._refresh_tokens.get(refresh_token)
        if stored is None or stored.client_id != client.client_id:
            return None
        return stored

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: MCPRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = self._refresh_tokens.pop(refresh_token.token, None)
        if stored is None or stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "Refresh token is invalid or already used.")
        if not stored.subject:
            raise TokenError("invalid_grant", "Refresh token has no resource owner.")
        return self._issue_token_pair(
            client_id=stored.client_id,
            subject=stored.subject,
            scopes=scopes,
            resource=stored.resource,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                issuer=self.config.issuer_url,
                audience=self.config.resource_url,
                leeway=30,
                options={"require": ["iss", "aud", "sub", "client_id", "scope", "iat", "exp", "jti"]},
            )
        except jwt.PyJWTError:
            return None

        scopes = claims["scope"].split() if isinstance(claims["scope"], str) else []
        if self.config.required_scope not in scopes:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims["client_id"]),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.config.resource_url,
            subject=str(claims["sub"]),
            claims=claims,
        )

    async def revoke_token(self, token: AccessToken | MCPRefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        numbers = self._public_key.public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.config.signing_key_id,
                    "n": _b64url_uint(numbers.n),
                    "e": _b64url_uint(numbers.e),
                }
            ]
        }

    def _issue_token_pair(self, *, client_id: str, subject: str, scopes: list[str], resource: str) -> OAuthToken:
        now = int(time.time())
        claims = {
            "iss": self.config.issuer_url,
            "aud": resource,
            "sub": subject,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + self.config.access_token_ttl_seconds,
            "jti": secrets.token_urlsafe(24),
        }
        access_token = jwt.encode(
            claims,
            self._private_key,
            algorithm="RS256",
            headers={"kid": self.config.signing_key_id, "typ": "at+jwt"},
        )
        refresh_value = f"mcp_refresh_{secrets.token_urlsafe(32)}"
        self._refresh_tokens[refresh_value] = MCPRefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + self.config.refresh_token_ttl_seconds,
            subject=subject,
            resource=resource,
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self.config.access_token_ttl_seconds,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    def _cleanup(self) -> None:
        now = time.time()
        self._pending_logins = {key: value for key, value in self._pending_logins.items() if value.expires_at >= now}
        self._pending_consents = {
            key: value for key, value in self._pending_consents.items() if value.expires_at >= now
        }
        self._authorization_codes = {
            key: value for key, value in self._authorization_codes.items() if value.expires_at >= now
        }
        self._refresh_tokens = {
            key: value
            for key, value in self._refresh_tokens.items()
            if value.expires_at is None or value.expires_at >= now
        }

    @staticmethod
    def _client_error_redirect(
        pending: PendingAuthorization, error: str, description: str
    ) -> RedirectResponse:
        return RedirectResponse(
            construct_redirect_uri(
                pending.redirect_uri,
                error=error,
                error_description=description,
                state=pending.client_state,
            ),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def _consent_page(self, consent_token: str, email: str) -> HTMLResponse:
        action = f"{self.config.issuer_url}/oauth/consent"
        content = f"""<!doctype html>
<html lang="zh-Hant">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>連接 GA4 Analytics</title></head>
<body style="font-family:system-ui;max-width:560px;margin:48px auto;padding:0 20px;line-height:1.55">
  <h1>連接 GA4 Analytics</h1>
  <p>登入帳號：<strong>{escape(email)}</strong></p>
  <p>Claude 將取得唯讀權限，能呼叫 <code>traffic_summary</code> 查詢已授權 tenant 的 GA4 流量摘要。</p>
  <form action="{escape(action)}" method="post">
    <input type="hidden" name="consent_token" value="{escape(consent_token)}">
    <button name="decision" value="approve" type="submit">允許</button>
    <button name="decision" value="deny" type="submit">拒絕</button>
  </form>
</body></html>"""
        return HTMLResponse(
            content,
            headers={
                "Cache-Control": "no-store",
                # Chrome applies form-action to redirects after a form POST.
                # The consent POST stays on this origin, then redirects to the
                # one pre-registered Claude callback to finish OAuth.
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "form-action 'self' https://claude.ai; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )


def jwks_response(provider: GoogleOAuthAuthorizationServer) -> JSONResponse:
    return JSONResponse(provider.jwks(), headers={"Cache-Control": "public, max-age=300"})
