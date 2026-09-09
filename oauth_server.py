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
from capability_registry import capability_registry


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
        metadata = capability_registry.consent_metadata()
        capability_cards = "".join(
            f"""
            <li class="capability-card">
              <div class="capability-card__heading">
                <h3>{escape(str(capability["label"]))}</h3>
                <span class="capability-card__status">可用</span>
              </div>
              <p>{escape(str(capability["description"]))}</p>
              <p class="capability-card__tools"><span>公開工具：</span><code>{escape(", ".join(capability["tools"]))}</code></p>
            </li>
            """
            for capability in metadata["supported"]
        )
        unsupported_items = "".join(
            f"""
            <li>
              <strong>{escape(str(item["label"]))}</strong>
              <span>{escape(str(item["message"]))}</span>
            </li>
            """
            for item in metadata["unsupported"]
        )
        limitation_items = "".join(
            f"<li>{escape(str(limitation))}</li>"
            for limitation in metadata["limitations"]
        )
        registry_version = escape(str(metadata["registry_version"]))
        required_scope = escape(self.config.required_scope)
        content = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>連接 GA4 Analytics</title>
  <style>
    :root {{
      color-scheme: light;
      --page: #f5f1e9;
      --card: #fffdf9;
      --ink: #282725;
      --muted: #6f6b63;
      --line: #ded8cc;
      --soft: #f1ede5;
      --accent: #2f5149;
      --accent-hover: #25443d;
      --deny: #5d5a54;
      --deny-hover: #45423d;
    }}

    * {{ box-sizing: border-box; }}

    body {{
      min-width: 320px;
      margin: 0;
      background: var(--page);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
    }}

    .page-shell {{
      display: grid;
      min-height: 100vh;
      place-items: center;
      padding: clamp(24px, 7vw, 72px) 16px;
    }}

    .card {{
      width: min(100%, 640px);
      overflow: hidden;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 18px 50px rgb(67 57 43 / 10%);
    }}

    .card__header {{ padding: 34px 36px 24px; }}

    .eyebrow {{
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}

    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 10px; font-size: clamp(1.75rem, 5vw, 2.15rem); line-height: 1.2; letter-spacing: -0.025em; }}
    h2 {{ margin-bottom: 14px; font-size: 1rem; line-height: 1.3; }}
    h3 {{ margin-bottom: 4px; font-size: 0.98rem; line-height: 1.35; }}
    .intro {{ max-width: 52ch; margin-bottom: 0; color: var(--muted); }}

    .card__body {{ display: grid; gap: 26px; padding: 0 36px 32px; }}

    .account, .scope {{
      padding: 16px 18px;
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 12px;
    }}

    .field-label {{ margin-bottom: 2px; color: var(--muted); font-size: 0.78rem; font-weight: 700; }}
    .account__email {{ margin-bottom: 0; overflow-wrap: anywhere; font-weight: 650; }}
    .scope {{ display: grid; grid-template-columns: minmax(96px, 0.7fr) 1.3fr; gap: 8px 16px; margin: 0; }}
    .scope dt {{ color: var(--muted); font-size: 0.82rem; font-weight: 700; }}
    .scope dd {{ margin: 0; font-size: 0.9rem; }}
    code {{
      padding: 2px 6px;
      background: rgb(40 39 37 / 7%);
      border-radius: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.82em;
    }}

    .capability-list, .limitation-list, .unsupported-list {{ display: grid; gap: 10px; margin: 0; padding: 0; list-style: none; }}
    .capability-card {{ padding: 15px 16px; border: 1px solid var(--line); border-radius: 11px; }}
    .capability-card__heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }}
    .capability-card p {{ margin-bottom: 6px; color: var(--muted); font-size: 0.9rem; }}
    .capability-card__status {{ flex: 0 0 auto; color: var(--accent); font-size: 0.75rem; font-weight: 700; }}
    .capability-card__tools {{ margin-bottom: 0 !important; }}
    .capability-card__tools span {{ margin-right: 5px; }}
    .limitation-list li, .unsupported-list li {{ display: grid; gap: 2px; padding-left: 18px; color: var(--muted); font-size: 0.9rem; }}
    .limitation-list li::before, .unsupported-list li::before {{
      position: absolute;
      margin-left: -17px;
      color: var(--deny);
      content: "—";
    }}
    .limitation-list li, .unsupported-list li {{ position: relative; }}
    .unsupported-list strong {{ color: var(--ink); font-size: 0.9rem; }}
    .audit-note {{ margin: 0; padding: 14px 16px; border-left: 3px solid var(--accent); background: rgb(47 81 73 / 6%); color: var(--muted); font-size: 0.88rem; }}
    .audit-note strong {{ color: var(--ink); }}

    .card__footer {{ padding: 24px 36px 32px; border-top: 1px solid var(--line); }}
    .decision-help {{ margin: 0 0 14px; color: var(--muted); font-size: 0.82rem; }}
    .actions {{ display: grid; grid-template-columns: 1fr 1.25fr; gap: 10px; margin: 0; padding: 0; border: 0; }}
    button {{
      min-height: 46px;
      padding: 10px 16px;
      border: 1px solid transparent;
      border-radius: 9px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: background-color 120ms ease, border-color 120ms ease;
    }}
    button:focus-visible {{ outline: 3px solid rgb(47 81 73 / 35%); outline-offset: 3px; }}
    .button--deny {{ background: transparent; border-color: var(--line); color: var(--deny); }}
    .button--deny:hover {{ background: var(--soft); border-color: #c8c0b2; color: var(--deny-hover); }}
    .button--approve {{ background: var(--accent); color: #fffdf9; }}
    .button--approve:hover {{ background: var(--accent-hover); }}
    .registry-note {{ margin: 16px 0 0; color: var(--muted); font-size: 0.72rem; text-align: center; }}
    .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}

    @media (max-width: 520px) {{
      .card__header {{ padding: 28px 22px 20px; }}
      .card__body {{ gap: 22px; padding: 0 22px 26px; }}
      .card__footer {{ padding: 20px 22px 24px; }}
      .scope {{ grid-template-columns: 1fr; gap: 2px; }}
      .scope dd {{ margin-bottom: 8px; }}
      .scope dd:last-child {{ margin-bottom: 0; }}
      .actions {{ grid-template-columns: 1fr; }}
      .button--approve {{ order: -1; }}
    }}

    @media (prefers-reduced-motion: reduce) {{
      button {{ transition: none; }}
    }}
  </style>
</head>
<body>
  <main class="page-shell">
    <section class="card" aria-labelledby="page-title" aria-describedby="page-intro">
      <header class="card__header">
        <p class="eyebrow">GA4 Analytics Service</p>
        <h1 id="page-title">連接 GA4 Analytics</h1>
        <p id="page-intro" class="intro">請確認要讓這個 connector 讀取已授權客戶的 GA4 資料。你可以隨時拒絕這次連接。</p>
      </header>

      <div class="card__body">
        <div class="account">
          <p class="field-label">登入帳號</p>
          <p class="account__email">{escape(email)}</p>
        </div>

        <dl class="scope">
          <dt>權限範圍</dt>
          <dd><code>{required_scope}</code> · 僅限讀取</dd>
          <dt>資料類型</dt>
          <dd>客戶清單與已授權範圍內的 GA4 analytics data</dd>
        </dl>

        <section aria-labelledby="can-do-title">
          <h2 id="can-do-title">這個 connector 可以做什麼</h2>
          <ul class="capability-list">{capability_cards}
          </ul>
        </section>

        <section aria-labelledby="limits-title">
          <h2 id="limits-title">明確限制</h2>
          <ul class="unsupported-list">{unsupported_items}
          </ul>
          <ul class="limitation-list" style="margin-top: 12px;">{limitation_items}
          </ul>
        </section>

        <p class="audit-note"><strong>查詢查核：</strong>只有在你明確要求時，才會提供實際 query provenance（SQL、參數與 BigQuery job metadata）。</p>
      </div>

      <footer class="card__footer">
        <p id="decision-help" class="decision-help">允許後，connector 會依上述唯讀能力處理查詢；不會取得或執行任意 SQL。</p>
        <form action="{escape(action)}" method="post">
          <input type="hidden" name="consent_token" value="{escape(consent_token)}">
          <fieldset class="actions">
            <legend class="sr-only">選擇是否允許連接</legend>
            <button class="button--deny" name="decision" value="deny" type="submit">拒絕並返回</button>
            <button class="button--approve" name="decision" value="approve" type="submit">允許連接</button>
          </fieldset>
        </form>
        <p class="registry-note">Capability registry v{registry_version}</p>
      </footer>
    </section>
  </main>
</body>
</html>"""
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
