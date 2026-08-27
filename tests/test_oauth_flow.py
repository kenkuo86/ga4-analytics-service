from __future__ import annotations

import base64
import hashlib
import os
import re
import time
import unittest
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


SERVICE_ORIGIN = "https://ga4-analytics-service-398991472921.asia-east1.run.app"
RESOURCE_URL = f"{SERVICE_ORIGIN}/mcp"
LEGACY_RESOURCE_URL = f"{RESOURCE_URL}/"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
HOST = "ga4-analytics-service-398991472921.asia-east1.run.app"


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


os.environ.update(
    {
        "AUTH_MODE": "oauth",
        "OAUTH_ISSUER_URL": SERVICE_ORIGIN,
        "MCP_PUBLIC_URL": RESOURCE_URL,
        "OAUTH_ALLOWED_EMAILS": "owner@example.com",
        "MCP_OAUTH_CLIENT_ID": "claude-web-test",
        "GOOGLE_OAUTH_CLIENT_ID": "google-test-client",
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-test-secret",
        "MCP_TOKEN_SIGNING_PRIVATE_KEY": _private_key_pem(),
    }
)

from starlette.testclient import TestClient  # noqa: E402

from mcp_server import app  # noqa: E402
from oauth_auth import oauth_runtime  # noqa: E402


class FakeGoogleIdentity:
    def __init__(self, email: str = "owner@example.com"):
        self.email = email
        self.expected_nonce: str | None = None

    async def exchange_code(self, code: str, expected_nonce: str):
        if code != "google-code":
            raise ValueError("unexpected Google code")
        self.expected_nonce = expected_nonce
        return {
            "sub": "google-user-123",
            "email": self.email,
            "email_verified": True,
            "nonce": expected_nonce,
        }


class OAuthFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert oauth_runtime.provider is not None
        cls.provider = oauth_runtime.provider
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.provider.google_identity = FakeGoogleIdentity()
        self.code_verifier = "v" * 64
        digest = hashlib.sha256(self.code_verifier.encode()).digest()
        self.code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        self.headers = {"host": HOST}

    def _begin_authorization(self, **overrides):
        params = {
            "client_id": "claude-web-test",
            "redirect_uri": CLAUDE_REDIRECT,
            "response_type": "code",
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "state": "claude-state",
            "scope": "ga4:read",
            "resource": RESOURCE_URL,
        }
        params.update(overrides)
        params = {key: value for key, value in params.items() if value is not None}
        return self.client.get("/authorize", params=params, headers=self.headers, follow_redirects=False)

    def _authorize_and_consent(self) -> str:
        authorize = self._begin_authorization()
        self.assertEqual(authorize.status_code, 302, authorize.text)
        google_query = parse_qs(urlparse(authorize.headers["location"]).query)
        self.assertEqual(google_query["nonce"][0], self.provider._pending_logins[google_query["state"][0]].google_nonce)

        callback = self.client.get(
            "/oauth/google/callback",
            params={"state": google_query["state"][0], "code": "google-code"},
            headers=self.headers,
        )
        self.assertEqual(callback.status_code, 200, callback.text)
        self.assertIn(
            "form-action 'self' https://claude.ai",
            callback.headers["content-security-policy"],
        )
        token_match = re.search(r'name="consent_token" value="([^"]+)"', callback.text)
        self.assertIsNotNone(token_match)

        consent = self.client.post(
            "/oauth/consent",
            data={"consent_token": token_match.group(1), "decision": "approve"},
            headers=self.headers,
            follow_redirects=False,
        )
        self.assertEqual(consent.status_code, 302, consent.text)
        redirect = urlparse(consent.headers["location"])
        self.assertEqual(f"{redirect.scheme}://{redirect.netloc}{redirect.path}", CLAUDE_REDIRECT)
        query = parse_qs(redirect.query)
        self.assertEqual(query["state"], ["claude-state"])
        return query["code"][0]

    def _exchange_code(self, code: str, **overrides):
        data = {
            "grant_type": "authorization_code",
            "client_id": "claude-web-test",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_verifier": self.code_verifier,
            "resource": RESOURCE_URL,
        }
        data.update(overrides)
        return self.client.post("/token", data=data, headers=self.headers)

    def test_discovery_and_unauthenticated_endpoints(self):
        health = self.client.get("/health", headers=self.headers)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        metadata = self.client.get("/.well-known/oauth-authorization-server", headers=self.headers)
        self.assertEqual(metadata.status_code, 200)
        body = metadata.json()
        self.assertEqual(body["issuer"], SERVICE_ORIGIN)
        self.assertEqual(body["authorization_endpoint"], f"{SERVICE_ORIGIN}/authorize")
        self.assertNotIn("registration_endpoint", body)
        self.assertIn("S256", body["code_challenge_methods_supported"])
        self.assertIn("none", body["token_endpoint_auth_methods_supported"])

        protected = self.client.get("/.well-known/oauth-protected-resource/mcp/", headers=self.headers)
        self.assertEqual(protected.status_code, 200)
        self.assertEqual(protected.json()["resource"], RESOURCE_URL)

        canonical_protected = self.client.get(
            "/.well-known/oauth-protected-resource/mcp", headers=self.headers
        )
        self.assertEqual(canonical_protected.status_code, 200)

        jwks = self.client.get("/.well-known/jwks.json", headers=self.headers)
        self.assertEqual(jwks.status_code, 200)
        self.assertEqual(jwks.json()["keys"][0]["alg"], "RS256")

        mcp = self.client.post("/mcp/", headers=self.headers, json={})
        self.assertEqual(mcp.status_code, 401)
        self.assertIn("resource_metadata=", mcp.headers["www-authenticate"])

        canonical_mcp = self.client.post("/mcp", headers=self.headers, json={})
        self.assertEqual(canonical_mcp.status_code, 401)

        rest = self.client.get("/traffic-summary", headers=self.headers)
        self.assertEqual(rest.status_code, 401)

    def test_full_flow_mcp_and_refresh_rotation(self):
        code = self._authorize_and_consent()
        token_response = self._exchange_code(code)
        self.assertEqual(token_response.status_code, 200, token_response.text)
        tokens = token_response.json()

        access = self.client.post(
            "/mcp",
            headers={
                **self.headers,
                "authorization": f"Bearer {tokens['access_token']}",
                "accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        self.assertEqual(access.status_code, 200, access.text)
        session_headers = {
            **self.headers,
            "authorization": f"Bearer {tokens['access_token']}",
            "accept": "application/json, text/event-stream",
            "mcp-session-id": access.headers["mcp-session-id"],
        }
        initialized = self.client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertEqual(initialized.status_code, 202, initialized.text)
        tools = self.client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(tools.status_code, 200, tools.text)
        self.assertEqual([tool["name"] for tool in tools.json()["result"]["tools"]], ["traffic_summary"])

        authorized_rest = self.client.get(
            "/traffic-summary",
            headers={**self.headers, "authorization": f"Bearer {tokens['access_token']}"},
        )
        self.assertEqual(authorized_rest.status_code, 422)

        reused_code = self._exchange_code(code)
        self.assertEqual(reused_code.status_code, 400)
        self.assertEqual(reused_code.json()["error"], "invalid_grant")

        refresh = self.client.post(
            "/token",
            headers=self.headers,
            data={
                "grant_type": "refresh_token",
                "client_id": "claude-web-test",
                "refresh_token": tokens["refresh_token"],
                "scope": "ga4:read",
                "resource": RESOURCE_URL,
            },
        )
        self.assertEqual(refresh.status_code, 200, refresh.text)
        self.assertNotEqual(refresh.json()["refresh_token"], tokens["refresh_token"])

        reused_refresh = self.client.post(
            "/token",
            headers=self.headers,
            data={
                "grant_type": "refresh_token",
                "client_id": "claude-web-test",
                "refresh_token": tokens["refresh_token"],
            },
        )
        self.assertEqual(reused_refresh.status_code, 400)
        self.assertEqual(reused_refresh.json()["error"], "invalid_grant")

    def test_wrong_resource_redirect_and_wrong_callback_rejected(self):
        missing_resource = self._begin_authorization(resource=None)
        self.assertEqual(missing_resource.status_code, 302)
        self.assertEqual(urlparse(missing_resource.headers["location"]).netloc, "accounts.google.com")

        trailing_slash_resource = self._begin_authorization(resource=LEGACY_RESOURCE_URL)
        self.assertEqual(trailing_slash_resource.status_code, 302)
        self.assertEqual(urlparse(trailing_slash_resource.headers["location"]).netloc, "accounts.google.com")

        bad_resource = self._begin_authorization(resource="https://attacker.example/mcp/")
        self.assertEqual(bad_resource.status_code, 302)
        self.assertEqual(parse_qs(urlparse(bad_resource.headers["location"]).query)["error"], ["invalid_target"])

        bad_redirect = self._begin_authorization(redirect_uri="https://attacker.example/callback")
        self.assertEqual(bad_redirect.status_code, 400)

    def test_unapproved_google_email_and_state_replay_rejected(self):
        self.provider.google_identity = FakeGoogleIdentity("intruder@example.com")
        authorize = self._begin_authorization()
        google_state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]

        denied = self.client.get(
            "/oauth/google/callback",
            params={"state": google_state, "code": "google-code"},
            headers=self.headers,
            follow_redirects=False,
        )
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(parse_qs(urlparse(denied.headers["location"]).query)["error"], ["access_denied"])

        replay = self.client.get(
            "/oauth/google/callback",
            params={"state": google_state, "code": "google-code"},
            headers=self.headers,
        )
        self.assertEqual(replay.status_code, 400)

    def test_wrong_client_and_pkce_rejected(self):
        code = self._authorize_and_consent()

        wrong_client = self._exchange_code(code, client_id="wrong-client")
        self.assertEqual(wrong_client.status_code, 401)
        self.assertEqual(wrong_client.json()["error"], "invalid_client")

        wrong_pkce = self._exchange_code(code, code_verifier="x" * 64)
        self.assertEqual(wrong_pkce.status_code, 400)
        self.assertEqual(wrong_pkce.json()["error"], "invalid_grant")


if __name__ == "__main__":
    unittest.main()
